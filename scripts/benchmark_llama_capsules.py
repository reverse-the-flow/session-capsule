#!/usr/bin/env python3
"""Benchmark replay against llama.cpp slot save/restore."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request


JSONDict = dict[str, Any]


@dataclass
class Scenario:
    scenario_name: str
    origin: str
    why: str
    messages: list[JSONDict]
    step_deltas: list[list[JSONDict]]
    ambiguity_notes: list[str]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a minimal replay-versus-capsule benchmark against llama.cpp."
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        default=Path("data/scenarios/research_loop_small.json"),
        help="Scenario JSON describing the base transcript and per-step deltas.",
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--chat-path", default="/v1/chat/completions")
    parser.add_argument("--replay-slot", type=int, default=0)
    parser.add_argument("--source-slot", type=int, default=1)
    parser.add_argument("--restore-slot", type=int, default=2)
    parser.add_argument(
        "--slot-field",
        default="id_slot",
        help="Request field used by the server to select a slot, for example id_slot or slot_id.",
    )
    parser.add_argument(
        "--replay-reset-action",
        default="",
        help="Optional slot action to run before each replay request, for example erase.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Generation budget per request. Prompt-only evaluation is preferred for the first MVP.",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/runs"),
        help="Directory where run folders should be created.",
    )
    parser.add_argument(
        "--label",
        default="mvp",
        help="Short label that becomes part of the run directory name.",
    )
    return parser.parse_args()


def load_scenario(path: Path) -> Scenario:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["scenario_name", "origin", "why", "messages", "step_deltas"]
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"Scenario is missing required fields: {', '.join(missing)}")

    ambiguity_notes = data.get("ambiguity_notes", [])
    if not isinstance(ambiguity_notes, list):
        raise ValueError("ambiguity_notes must be a list if present")

    return Scenario(
        scenario_name=data["scenario_name"],
        origin=data["origin"],
        why=data["why"],
        messages=validate_messages(data["messages"], "messages"),
        step_deltas=[
            validate_messages(item, f"step_deltas[{index}]")
            for index, item in enumerate(data["step_deltas"], start=1)
        ],
        ambiguity_notes=[str(item) for item in ambiguity_notes],
    )


def validate_messages(messages: Any, field_name: str) -> list[JSONDict]:
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{field_name} must be a non-empty list of chat messages")

    validated: list[JSONDict] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise ValueError(f"{field_name}[{index}] must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError(f"{field_name}[{index}] must include string role and content")
        validated.append({"role": role, "content": content})
    return validated


def build_transcripts(base_messages: list[JSONDict], step_deltas: list[list[JSONDict]]) -> list[list[JSONDict]]:
    running = [dict(message) for message in base_messages]
    transcripts: list[list[JSONDict]] = []
    for delta in step_deltas:
        running.extend(dict(message) for message in delta)
        transcripts.append([dict(message) for message in running])
    return transcripts


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: JSONDict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: JSONDict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def post_json(url: str, payload: JSONDict, timeout: float) -> tuple[JSONDict, float]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc.reason}") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return json.loads(body or "{}"), elapsed_ms


def get_json(url: str, timeout: float) -> tuple[Any, float]:
    req = request.Request(url, method="GET")
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc.reason}") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return json.loads(body or "null"), elapsed_ms


def chat_completion(
    base_url: str,
    chat_path: str,
    slot_field: str,
    slot_id: int,
    messages: list[JSONDict],
    cache_prompt: bool,
    max_tokens: int,
    temperature: float,
    seed: int,
    timeout: float,
) -> tuple[JSONDict, float]:
    payload: JSONDict = {
        "messages": messages,
        "stream": False,
        "cache_prompt": cache_prompt,
        "temperature": temperature,
        "seed": seed,
        "max_tokens": max_tokens,
    }
    payload[slot_field] = slot_id
    return post_json(f"{base_url.rstrip('/')}{chat_path}", payload, timeout)


def slot_action(
    base_url: str,
    slot_id: int,
    action: str,
    timeout: float,
    filename: Path | None = None,
) -> tuple[JSONDict, float]:
    payload: JSONDict = {}
    if filename is not None:
        payload["filename"] = str(filename)
    return post_json(
        f"{base_url.rstrip('/')}/slots/{slot_id}?action={action}",
        payload,
        timeout,
    )


def coerce_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_response(response: Any) -> JSONDict:
    if not isinstance(response, dict):
        return {"response_type": type(response).__name__}

    timings = response.get("timings", {})
    usage = response.get("usage", {})
    choices = response.get("choices", [])
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""

    return {
        "usage": usage if isinstance(usage, dict) else {},
        "timings": timings if isinstance(timings, dict) else {},
        "finish_reason": first_choice.get("finish_reason") if isinstance(first_choice, dict) else None,
        "response_text_chars": len(content) if isinstance(content, str) else 0,
    }


def metric_block(values: list[float]) -> JSONDict:
    if not values:
        return {"count": 0, "total": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "total": round(sum(values), 3),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def collect_numbers(events: list[JSONDict], event_type: str, path: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for event in events:
        if event.get("event_type") != event_type:
            continue
        current: Any = event
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        number = coerce_number(current)
        if number is not None:
            values.append(number)
    return values


def aggregate_summary(
    run_id: str,
    run_dir: Path,
    scenario: Scenario,
    manifest: JSONDict,
    events: list[JSONDict],
) -> JSONDict:
    replay_client_ms = collect_numbers(events, "replay_completion", ("client_duration_ms",))
    replay_prompt_ms = collect_numbers(events, "replay_completion", ("response", "timings", "prompt_ms"))
    replay_prompt_tokens = collect_numbers(events, "replay_completion", ("response", "usage", "prompt_tokens"))

    restore_client_ms = collect_numbers(events, "capsule_restore", ("client_duration_ms",))
    capsule_client_ms = collect_numbers(events, "capsule_completion", ("client_duration_ms",))
    save_client_ms = collect_numbers(events, "capsule_save", ("client_duration_ms",))
    capsule_prompt_ms = collect_numbers(events, "capsule_completion", ("response", "timings", "prompt_ms"))
    capsule_prompt_tokens = collect_numbers(events, "capsule_completion", ("response", "usage", "prompt_tokens"))

    combined_capsule_client_total = sum(restore_client_ms) + sum(capsule_client_ms) + sum(save_client_ms)
    combined_capsule_prompt_total = sum(capsule_prompt_ms)
    replay_client_total = sum(replay_client_ms)
    replay_prompt_total = sum(replay_prompt_ms)

    client_reduction = None
    if replay_client_total:
        client_reduction = round(1 - (combined_capsule_client_total / replay_client_total), 6)

    prompt_reduction = None
    if replay_prompt_total:
        prompt_reduction = round(1 - (combined_capsule_prompt_total / replay_prompt_total), 6)

    return {
        "created_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "status": "completed",
        "scenario_name": scenario.scenario_name,
        "step_count": len(scenario.step_deltas),
        "source_path": manifest["source_path"],
        "origin": scenario.origin,
        "why": scenario.why,
        "ambiguity_notes": manifest["ambiguity_notes"],
        "config": manifest["config"],
        "replay": {
            "client_wall_ms": metric_block(replay_client_ms),
            "server_prompt_ms": metric_block(replay_prompt_ms),
            "prompt_tokens": metric_block(replay_prompt_tokens),
        },
        "capsule": {
            "restore_wall_ms": metric_block(restore_client_ms),
            "completion_wall_ms": metric_block(capsule_client_ms),
            "save_wall_ms": metric_block(save_client_ms),
            "combined_client_wall_ms": round(combined_capsule_client_total, 3),
            "server_prompt_ms": metric_block(capsule_prompt_ms),
            "prompt_tokens": metric_block(capsule_prompt_tokens),
        },
        "reductions": {
            "client_wall_reduction_fraction": client_reduction,
            "server_prompt_reduction_fraction": prompt_reduction,
        },
    }


def log_event(
    events: list[JSONDict],
    path: Path,
    *,
    event_type: str,
    step: int | None,
    summary: str,
    transformation: str,
    why: str,
    ambiguity_notes: list[str],
    client_duration_ms: float,
    response: JSONDict | None = None,
    artifact_path: Path | None = None,
    request_meta: JSONDict | None = None,
) -> None:
    payload: JSONDict = {
        "created_at": now_iso(),
        "event_type": event_type,
        "step": step,
        "summary": summary,
        "transformation": transformation,
        "why": why,
        "ambiguity_notes": ambiguity_notes,
        "client_duration_ms": client_duration_ms,
    }
    if response is not None:
        payload["response"] = response
    if artifact_path is not None:
        payload["artifact_path"] = str(artifact_path.resolve())
    if request_meta is not None:
        payload["request"] = request_meta
    events.append(payload)
    append_jsonl(path, payload)


def main() -> None:
    args = parse_args()
    scenario_path = args.scenario.resolve()
    scenario = load_scenario(scenario_path)
    transcripts = build_transcripts(scenario.messages, scenario.step_deltas)

    created_at = datetime.now().astimezone()
    run_id = f"{created_at.strftime('%Y%m%d-%H%M%S')}-{args.label}"
    run_dir = args.output_dir.resolve() / run_id
    capsules_dir = run_dir / "capsules"
    ensure_dir(capsules_dir)

    events_path = run_dir / "events.jsonl"
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.json"

    manifest: JSONDict = {
        "created_at": created_at.isoformat(timespec="seconds"),
        "run_id": run_id,
        "source_path": str(scenario_path),
        "origin": scenario.origin,
        "transformation": "Turn a fixed transcript-growth scenario into paired replay and llama.cpp slot save/restore measurements.",
        "why": scenario.why,
        "ambiguity_notes": scenario.ambiguity_notes
        + [
            f"The slot-selection field is configured as {args.slot_field}.",
            "Replay measurements assume cache_prompt=false is sufficient unless a replay reset action is configured.",
            "This MVP assumes prompt-only evaluation works when max_tokens is 0 so slot state stays aligned to the canonical transcript.",
        ],
        "config": {
            "base_url": args.base_url,
            "chat_path": args.chat_path,
            "replay_slot": args.replay_slot,
            "source_slot": args.source_slot,
            "restore_slot": args.restore_slot,
            "slot_field": args.slot_field,
            "replay_reset_action": args.replay_reset_action or None,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "seed": args.seed,
            "timeout": args.timeout,
        },
    }

    slots_response, slots_ms = get_json(f"{args.base_url.rstrip('/')}/slots", args.timeout)
    manifest["initial_slots"] = {
        "client_duration_ms": slots_ms,
        "response": slots_response,
    }
    write_json(manifest_path, manifest)

    events: list[JSONDict] = []

    warmup_response, warmup_ms = chat_completion(
        args.base_url,
        args.chat_path,
        args.slot_field,
        args.replay_slot,
        scenario.messages,
        False,
        args.max_tokens,
        args.temperature,
        args.seed,
        args.timeout,
    )
    log_event(
        events,
        events_path,
        event_type="warmup_completion",
        step=None,
        summary="Warm the server before measured replay and capsule requests.",
        transformation="POST chat completion with the shared seed transcript and cache reuse disabled.",
        why="Reduce one-time cold-start effects before the benchmark loop.",
        ambiguity_notes=[],
        client_duration_ms=warmup_ms,
        response=summarize_response(warmup_response),
        request_meta={"slot": args.replay_slot, "message_count": len(scenario.messages), "cache_prompt": False},
    )

    seed_response, seed_ms = chat_completion(
        args.base_url,
        args.chat_path,
        args.slot_field,
        args.source_slot,
        scenario.messages,
        True,
        args.max_tokens,
        args.temperature,
        args.seed,
        args.timeout,
    )
    log_event(
        events,
        events_path,
        event_type="capsule_seed_completion",
        step=0,
        summary="Prefill the shared prompt prefix into the source slot.",
        transformation="POST chat completion with the base transcript and cache reuse enabled on the capsule source slot.",
        why="Create the first resumable prompt state before stepping through transcript growth.",
        ambiguity_notes=[
            "If max_tokens is not treated as prompt-only evaluation by the server, the saved slot may drift from the canonical transcript."
        ],
        client_duration_ms=seed_ms,
        response=summarize_response(seed_response),
        request_meta={"slot": args.source_slot, "message_count": len(scenario.messages), "cache_prompt": True},
    )

    seed_snapshot = capsules_dir / "step-000.bin"
    seed_save_response, seed_save_ms = slot_action(
        args.base_url,
        args.source_slot,
        "save",
        args.timeout,
        seed_snapshot,
    )
    log_event(
        events,
        events_path,
        event_type="capsule_seed_save",
        step=0,
        summary="Save the initial slot snapshot for the shared prompt prefix.",
        transformation="POST /slots/{source_slot}?action=save for the seeded capsule slot.",
        why="Use the saved seed state as the baseline restore point for step 1.",
        ambiguity_notes=[],
        client_duration_ms=seed_save_ms,
        response=summarize_response(seed_save_response),
        artifact_path=seed_snapshot,
        request_meta={"slot": args.source_slot, "action": "save"},
    )

    active_snapshot = seed_snapshot
    active_restore_slot = args.restore_slot

    for step_index, transcript in enumerate(transcripts, start=1):
        if args.replay_reset_action:
            reset_response, reset_ms = slot_action(
                args.base_url,
                args.replay_slot,
                args.replay_reset_action,
                args.timeout,
            )
            log_event(
                events,
                events_path,
                event_type="replay_reset",
                step=step_index,
                summary="Reset the replay slot before the full transcript replay request.",
                transformation=f"POST /slots/{{replay_slot}}?action={args.replay_reset_action}.",
                why="Reduce the chance that replay measurements inherit slot state from an earlier request.",
                ambiguity_notes=[],
                client_duration_ms=reset_ms,
                response=summarize_response(reset_response),
                request_meta={"slot": args.replay_slot, "action": args.replay_reset_action},
            )

        replay_response, replay_ms = chat_completion(
            args.base_url,
            args.chat_path,
            args.slot_field,
            args.replay_slot,
            transcript,
            False,
            args.max_tokens,
            args.temperature,
            args.seed,
            args.timeout,
        )
        log_event(
            events,
            events_path,
            event_type="replay_completion",
            step=step_index,
            summary="Measure full transcript replay without capsule restore.",
            transformation="POST chat completion with the accumulated transcript and cache reuse disabled.",
            why="Capture the baseline cost of replaying the entire transcript at this step.",
            ambiguity_notes=[
                "Replay is only as clean as the server's handling of cache_prompt=false and any configured replay reset action."
            ],
            client_duration_ms=replay_ms,
            response=summarize_response(replay_response),
            request_meta={"slot": args.replay_slot, "message_count": len(transcript), "cache_prompt": False},
        )

        restore_response, restore_ms = slot_action(
            args.base_url,
            active_restore_slot,
            "restore",
            args.timeout,
            active_snapshot,
        )
        log_event(
            events,
            events_path,
            event_type="capsule_restore",
            step=step_index,
            summary="Restore the prior capsule snapshot into the restore slot.",
            transformation="POST /slots/{restore_slot}?action=restore for the previous step snapshot.",
            why="Resume the already-processed prefix before measuring the next full transcript request.",
            ambiguity_notes=[],
            client_duration_ms=restore_ms,
            response=summarize_response(restore_response),
            artifact_path=active_snapshot,
            request_meta={"slot": active_restore_slot, "action": "restore"},
        )

        capsule_response, capsule_ms = chat_completion(
            args.base_url,
            args.chat_path,
            args.slot_field,
            active_restore_slot,
            transcript,
            True,
            args.max_tokens,
            args.temperature,
            args.seed,
            args.timeout,
        )
        log_event(
            events,
            events_path,
            event_type="capsule_completion",
            step=step_index,
            summary="Measure the accumulated transcript after restoring the previous capsule snapshot.",
            transformation="POST chat completion with the accumulated transcript and cache reuse enabled on the restored slot.",
            why="Measure how much prompt work remains after state restoration.",
            ambiguity_notes=[],
            client_duration_ms=capsule_ms,
            response=summarize_response(capsule_response),
            request_meta={"slot": active_restore_slot, "message_count": len(transcript), "cache_prompt": True},
        )

        next_snapshot = capsules_dir / f"step-{step_index:03d}.bin"
        save_response, save_ms = slot_action(
            args.base_url,
            active_restore_slot,
            "save",
            args.timeout,
            next_snapshot,
        )
        log_event(
            events,
            events_path,
            event_type="capsule_save",
            step=step_index,
            summary="Save the updated slot snapshot for the next benchmark step.",
            transformation="POST /slots/{restore_slot}?action=save after the capsule completion.",
            why="Carry the updated prompt state forward as the next restore point.",
            ambiguity_notes=[],
            client_duration_ms=save_ms,
            response=summarize_response(save_response),
            artifact_path=next_snapshot,
            request_meta={"slot": active_restore_slot, "action": "save"},
        )

        active_snapshot = next_snapshot
        active_restore_slot = (
            args.source_slot if active_restore_slot == args.restore_slot else args.restore_slot
        )

    summary = aggregate_summary(run_id, run_dir, scenario, manifest, events)
    write_json(summary_path, summary)

    print(f"Run folder: {run_dir}")
    print(f"Scenario: {scenario.scenario_name}")
    print(f"Replay total wall ms: {summary['replay']['client_wall_ms']['total']}")
    print(f"Capsule total wall ms: {summary['capsule']['combined_client_wall_ms']}")
    print(
        "Client wall reduction: "
        f"{summary['reductions']['client_wall_reduction_fraction']}"
    )


if __name__ == "__main__":
    main()
