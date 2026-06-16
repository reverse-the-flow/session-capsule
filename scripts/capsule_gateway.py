#!/usr/bin/env python3
"""Local OpenAI-compatible Session Capsules gateway.

The gateway sits in the request path:

client -> capsule gateway -> configured model endpoint

It manages thread ledgers, optional prefill roots, restore/delta forwarding when
a hard capsule exists, response transcript capture, and post-response
checkpointing.
"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

import capsule_cli as cc


JSONDict = dict[str, Any]


THREAD_HEADER_CANDIDATES = [
    "X-Capsule-Thread",
    "X-OpenWebUI-Chat-Id",
    "X-Opencode-Thread",
    "X-Opencode-Session",
    "X-Session-Id",
    "X-Conversation-Id",
]

WORKSPACE_HEADER_CANDIDATES = [
    "X-Capsule-Workspace",
    "X-OpenWebUI-User-Id",
    "X-Opencode-Workspace",
    "X-Workspace-Id",
]


@dataclass
class GatewayConfig:
    state_dir: Path
    endpoint_id: str
    host: str
    port: int
    slot: int
    checkpoint_mode: str
    timeout: float
    default_prefill: str | None
    default_thread_prefix: str
    lock: threading.Lock


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def request_json(url: str, payload: JSONDict, timeout: float) -> tuple[int, dict[str, str], bytes]:
    encoded = json_bytes(payload)
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            headers = {key: value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except error.HTTPError as exc:
        return exc.code, {key: value for key, value in exc.headers.items()}, exc.read()


def safe_header_id(value: str | None) -> str | None:
    if value is None:
        return None
    return cc.slugify(value)


def first_header(handler: BaseHTTPRequestHandler, candidates: list[str]) -> tuple[str | None, str | None]:
    for header in candidates:
        value = handler.headers.get(header)
        if value:
            return header, value
    return None, None


def generated_thread_id(config: GatewayConfig, body: JSONDict) -> str:
    model = str(body.get("model", "unknown"))
    messages = body.get("messages", [])
    seed = model
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            seed += ":" + str(first.get("role", "")) + ":" + str(first.get("content", ""))[:120]
    digest = cc.digest_text(seed).split(":", 1)[1][:12]
    return f"{config.default_thread_prefix}-{digest}"


def read_body(handler: BaseHTTPRequestHandler) -> JSONDict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    data = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def send_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    body = json_bytes(payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_bytes(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    status: int,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def ensure_endpoint(config: GatewayConfig) -> JSONDict:
    store = cc.Store(config.state_dir)
    return store.load_endpoint(config.endpoint_id)


def endpoint_url(endpoint: JSONDict, path: str) -> str:
    return endpoint["base_url"].rstrip("/") + path


def ensure_thread(
    config: GatewayConfig,
    thread_id: str,
    workspace: str | None,
    prefill: str | None,
) -> JSONDict:
    store = cc.Store(config.state_dir)
    ledger_path = store.ledger_path(thread_id)
    if ledger_path.exists():
        return store.load_ledger(thread_id)

    args = argparse.Namespace(
        state_dir=config.state_dir,
        endpoint=config.endpoint_id,
        thread_id=thread_id,
        name=thread_id,
        workspace=workspace,
        prefill=prefill,
        prefill_version=None,
        force=False,
    )
    result = cc.thread_start(args)
    if result != 0:
        raise RuntimeError(f"Could not start thread {thread_id}")
    return store.load_ledger(thread_id)


def append_request_messages(config: GatewayConfig, thread_id: str, messages: list[JSONDict]) -> None:
    store = cc.Store(config.state_dir)
    existing = cc.read_jsonl(store.transcript_path(thread_id))
    existing_pairs = [(row.get("role"), row.get("content")) for row in existing]
    incoming_pairs = [
        (message.get("role"), message.get("content"))
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    ]

    prefix_len = 0
    while prefix_len < len(existing_pairs) and prefix_len < len(incoming_pairs):
        if existing_pairs[prefix_len] != incoming_pairs[prefix_len]:
            break
        prefix_len += 1

    if prefix_len < len(existing_pairs):
        # The client did not send a clean transcript prefix. Preserve evidence
        # by appending the whole request as new request metadata instead of
        # trying to surgically rewrite history.
        content = json.dumps(messages, sort_keys=True)
        args = argparse.Namespace(
            state_dir=config.state_dir,
            thread=thread_id,
            role="user",
            content=content,
            file=None,
        )
        cc.thread_append(args)
        return

    for message in messages[prefix_len:]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        role = str(message.get("role", "user"))
        if not isinstance(content, str):
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        args = argparse.Namespace(
            state_dir=config.state_dir,
            thread=thread_id,
            role=role,
            content=content,
            file=None,
        )
        cc.thread_append(args)


def append_assistant_response(config: GatewayConfig, thread_id: str, response_payload: JSONDict) -> None:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    first = choices[0]
    if not isinstance(first, dict):
        return
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        content = message["content"]
    elif isinstance(first.get("text"), str):
        content = first["text"]
    else:
        return
    args = argparse.Namespace(
        state_dir=config.state_dir,
        thread=thread_id,
        role="assistant",
        content=content,
        file=None,
    )
    cc.thread_append(args)


def latest_hard_manifest(config: GatewayConfig, thread_id: str) -> tuple[JSONDict, JSONDict] | None:
    store = cc.Store(config.state_dir)
    ledger = store.load_ledger(thread_id)
    try:
        return cc.find_latest_restorable_manifest(store, ledger, None)
    except Exception:  # noqa: BLE001 - absence of hard capsule means replay fallback.
        return None


def replay_messages(config: GatewayConfig, thread_id: str) -> list[JSONDict]:
    store = cc.Store(config.state_dir)
    ledger = store.load_ledger(thread_id)
    rows = cc.read_jsonl(store.transcript_path(thread_id))
    messages: list[JSONDict] = []

    active = ledger.get("active_capsule_id")
    parent = cc.find_capsule_link(ledger, active)
    if parent and str(parent.get("kind", "")).endswith("_prefill"):
        manifest = cc.load_manifest_ref(store, parent["manifest_ref"])
        source = manifest.get("prefill_source", {})
        source_ref = source.get("source_ref")
        if source_ref:
            source_path = store.root / source_ref
            if source_path.exists():
                messages.append({"role": "system", "content": source_path.read_text(encoding="utf-8")})

    for row in rows:
        messages.append({"role": row["role"], "content": row["content"]})
    return messages


def prepare_backend_body(config: GatewayConfig, body: JSONDict, thread_id: str, endpoint: JSONDict) -> tuple[JSONDict, str]:
    hard = latest_hard_manifest(config, thread_id)
    outbound = dict(body)
    outbound["stream"] = False

    if hard is None:
        outbound["messages"] = replay_messages(config, thread_id)
        outbound["cache_prompt"] = False
        return outbound, "replay"

    _link, manifest = hard
    cc.assert_manifest_compatible(manifest, endpoint)
    storage = manifest["storage"]
    runtime_snapshot_ref = storage.get("runtime_snapshot_ref") or storage.get("snapshot_ref")
    if not runtime_snapshot_ref:
        outbound["messages"] = replay_messages(config, thread_id)
        outbound["cache_prompt"] = False
        return outbound, "replay-missing-snapshot"

    cc.slot_action(endpoint, config.slot, "restore", runtime_snapshot_ref, config.timeout)
    rows = cc.read_jsonl(cc.Store(config.state_dir).transcript_path(thread_id))
    diff = cc.diff_messages_after(rows, int(manifest["context"]["token_end"]))
    outbound["messages"] = diff
    outbound["cache_prompt"] = True
    outbound[endpoint.get("slot_api", {}).get("slot_field", "id_slot")] = config.slot
    return outbound, "restore"


def checkpoint_after_response(config: GatewayConfig, thread_id: str) -> str | None:
    if config.checkpoint_mode == "none":
        return None
    if config.checkpoint_mode == "hard":
        capsule_id = cc.create_hard_checkpoint(
            cc.Store(config.state_dir),
            thread_id,
            config.slot,
            None,
            config.timeout,
            None,
        )
        return capsule_id
    args = argparse.Namespace(
        state_dir=config.state_dir,
        thread=thread_id,
        capsule_id=None,
    )
    cc.checkpoint_soft(args)
    ledger = cc.Store(config.state_dir).load_ledger(thread_id)
    return ledger.get("active_capsule_id")


def handle_chat_completion(handler: BaseHTTPRequestHandler, config: GatewayConfig, body: JSONDict) -> None:
    if body.get("stream") is True:
        send_json(
            handler,
            {
                "error": {
                    "message": "Streaming is not implemented in the capsule gateway v0. Send stream=false.",
                    "type": "unsupported_streaming",
                }
            },
            status=400,
        )
        return

    messages = body.get("messages")
    if not isinstance(messages, list):
        send_json(handler, {"error": {"message": "messages must be a list"}}, status=400)
        return

    endpoint = ensure_endpoint(config)
    _thread_header, thread_header_value = first_header(handler, THREAD_HEADER_CANDIDATES)
    _workspace_header, workspace = first_header(handler, WORKSPACE_HEADER_CANDIDATES)
    thread_id = safe_header_id(thread_header_value) or generated_thread_id(config, body)
    prefill = safe_header_id(handler.headers.get("X-Capsule-Prefill")) or config.default_prefill

    with config.lock:
        ensure_thread(config, thread_id, workspace, prefill)
        append_request_messages(config, thread_id, messages)
        outbound, mode = prepare_backend_body(config, body, thread_id, endpoint)
        status, headers, response_body = request_json(
            endpoint_url(endpoint, "/v1/chat/completions"),
            outbound,
            config.timeout,
        )
        response_payload: JSONDict | None = None
        if status < 400:
            try:
                parsed = json.loads(response_body.decode("utf-8"))
                if isinstance(parsed, dict):
                    response_payload = parsed
            except json.JSONDecodeError:
                response_payload = None
        checkpoint_id = None
        if response_payload is not None:
            append_assistant_response(config, thread_id, response_payload)
            checkpoint_id = checkpoint_after_response(config, thread_id)

    response_headers = {
        "X-Capsule-Thread": thread_id,
        "X-Capsule-Mode": mode,
    }
    if checkpoint_id:
        response_headers["X-Capsule-Checkpoint"] = checkpoint_id
    content_type = headers.get("Content-Type", "application/json")
    send_bytes(handler, response_body, status, content_type, response_headers)


def thread_summaries(config: GatewayConfig) -> list[JSONDict]:
    store = cc.Store(config.state_dir)
    if not store.threads_dir.exists():
        return []
    summaries: list[JSONDict] = []
    for ledger_path in sorted(store.threads_dir.glob("*/thread-ledger.json")):
        ledger = cc.read_json(ledger_path)
        summaries.append(
            {
                "thread_id": ledger["thread_id"],
                "endpoint_id": ledger["endpoint_id"],
                "active_capsule_id": ledger.get("active_capsule_id"),
                "capsule_count": len(ledger.get("capsules", [])),
                "open_diff_count": len(ledger.get("open_diffs", [])),
                "updated_at": ledger.get("updated_at"),
            }
        )
    return summaries


def checkpoint_from_api(config: GatewayConfig, payload: JSONDict) -> JSONDict:
    thread_id = str(payload["thread_id"])
    mode = str(payload.get("mode", config.checkpoint_mode))
    slot = int(payload.get("slot", config.slot))
    with config.lock:
        if mode == "hard":
            capsule_id = cc.create_hard_checkpoint(
                cc.Store(config.state_dir),
                thread_id,
                slot,
                None,
                config.timeout,
                None,
            )
        elif mode == "soft":
            args = argparse.Namespace(state_dir=config.state_dir, thread=thread_id, capsule_id=None)
            cc.checkpoint_soft(args)
            capsule_id = cc.Store(config.state_dir).load_ledger(thread_id).get("active_capsule_id")
        else:
            raise ValueError("mode must be soft or hard")
    return {"thread_id": thread_id, "capsule_id": capsule_id, "mode": mode}


def make_handler(config: GatewayConfig) -> type[BaseHTTPRequestHandler]:
    class CapsuleGatewayHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/capsules/status":
                endpoint = ensure_endpoint(config)
                send_json(
                    self,
                    {
                        "status": "ok",
                        "state_dir": str(config.state_dir),
                        "endpoint_id": config.endpoint_id,
                        "endpoint_base_url": endpoint["base_url"],
                        "checkpoint_mode": config.checkpoint_mode,
                        "threads": len(thread_summaries(config)),
                    },
                )
                return
            if self.path == "/api/capsules/threads":
                send_json(self, {"threads": thread_summaries(config)})
                return
            if self.path == "/v1/models":
                endpoint = ensure_endpoint(config)
                try:
                    with request.urlopen(endpoint_url(endpoint, "/v1/models"), timeout=config.timeout) as response:
                        send_bytes(
                            self,
                            response.read(),
                            response.status,
                            response.headers.get("Content-Type", "application/json"),
                        )
                except Exception:
                    send_json(self, {"data": []})
                return
            send_json(self, {"error": {"message": "not found"}}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = read_body(self)
                if self.path == "/v1/chat/completions":
                    handle_chat_completion(self, config, body)
                    return
                if self.path == "/api/capsules/checkpoint":
                    send_json(self, checkpoint_from_api(config, body))
                    return
                send_json(self, {"error": {"message": "not found"}}, status=404)
            except Exception as exc:  # noqa: BLE001 - gateway should return JSON errors.
                send_json(self, {"error": {"message": str(exc), "type": "gateway_error"}}, status=500)

    return CapsuleGatewayHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Session Capsules OpenAI-compatible gateway.")
    parser.add_argument("--state-dir", type=Path, default=Path(".capsules"))
    parser.add_argument("--endpoint", required=True, help="Endpoint id from the capsule state directory.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--slot", type=int, default=0)
    parser.add_argument("--checkpoint-mode", choices=["none", "soft", "hard"], default="soft")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--default-prefill")
    parser.add_argument("--default-thread-prefix", default="gateway")
    return parser


def create_server(config: GatewayConfig) -> ThreadingHTTPServer:
    handler = make_handler(config)
    return ThreadingHTTPServer((config.host, config.port), handler)


def main() -> int:
    args = build_parser().parse_args()
    config = GatewayConfig(
        state_dir=args.state_dir.resolve(),
        endpoint_id=args.endpoint,
        host=args.host,
        port=args.port,
        slot=args.slot,
        checkpoint_mode=args.checkpoint_mode,
        timeout=args.timeout,
        default_prefill=args.default_prefill,
        default_thread_prefix=args.default_thread_prefix,
        lock=threading.Lock(),
    )
    # Fail fast if endpoint is not configured.
    ensure_endpoint(config)
    server = create_server(config)
    print(f"capsule gateway listening on http://{config.host}:{config.port}")
    print(f"endpoint: {config.endpoint_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping capsule gateway")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
