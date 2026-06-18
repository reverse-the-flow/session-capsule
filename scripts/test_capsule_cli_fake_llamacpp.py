#!/usr/bin/env python3
"""Smoke test capsule_cli.py against a fake llama.cpp slot server."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "capsule_cli.py"


class FakeLlamaHandler(BaseHTTPRequestHandler):
    events: list[dict[str, Any]] = []
    fail_restore_once = False

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/slots":
            self.send_json(
                [
                    {"id": 0, "n_ctx": 8192, "is_processing": False},
                    {"id": 1, "n_ctx": 8192, "is_processing": False},
                ]
            )
            return
        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self.read_payload()
        FakeLlamaHandler.events.append(
            {"path": parsed.path, "query": parsed.query, "payload": payload}
        )

        if parsed.path.startswith("/slots/"):
            slot_id = int(parsed.path.strip("/").split("/")[1])
            action = parse_qs(parsed.query).get("action", [""])[0]
            filename = payload.get("filename")
            if action == "save":
                if filename:
                    path = Path(filename)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fake-slot-state")
                self.send_json(
                    {
                        "id_slot": slot_id,
                        "filename": filename,
                        "n_saved": 3,
                        "n_written": len(b"fake-slot-state"),
                        "timings": {"save_ms": 1.25},
                    }
                )
                return
            if action == "restore":
                if FakeLlamaHandler.fail_restore_once:
                    FakeLlamaHandler.fail_restore_once = False
                    self.send_json({"error": "forced restore failure"}, status=500)
                    return
                self.send_json(
                    {
                        "id_slot": slot_id,
                        "filename": filename,
                        "timings": {"restore_ms": 0.75},
                    }
                )
                return

        if parsed.path == "/v1/chat/completions":
            self.send_json(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": ""},
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 0},
                }
            )
            return

        self.send_json({"error": "not found"}, status=404)


def run_cli(state_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLI), "--state-dir", str(state_dir), *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def write_job(path: Path, job_type: str, params: dict[str, object]) -> None:
    payload = {
        "schema_version": "0.1",
        "job_id": path.stem,
        "job_type": job_type,
        "created_at": "2026-06-18T14:20:00-05:00",
        "requested_by": "fake-llamacpp-smoke",
        "params": params,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with tempfile.TemporaryDirectory(prefix="session-capsules-fake-") as temp:
            state = Path(temp) / ".capsules"
            jobs = Path(temp) / "jobs"
            jobs.mkdir()
            run_cli(
                state,
                "endpoint",
                "add",
                "local-llamacpp",
                "--type",
                "llamacpp",
                "--base-url",
                base_url,
                "--runtime-build",
                "fake-build",
                "--model-ref",
                "fake-model",
                "--model-hash",
                "sha256-fake-model",
                "--tokenizer-hash",
                "sha256-fake-tokenizer",
                "--context-limit",
                "8192",
            )
            run_cli(state, "endpoint", "doctor", "local-llamacpp", "--strict")
            source_path = Path(temp) / "user_prefill.md"
            source_path.write_text("Stable user prefill for fake runtime.", encoding="utf-8")
            run_cli(
                state,
                "prefill",
                "create",
                "--endpoint",
                "local-llamacpp",
                "--name",
                "user_default",
                "--input",
                str(source_path),
                "--hard",
                "--slot",
                "0",
            )
            run_cli(
                state,
                "thread",
                "start",
                "--endpoint",
                "local-llamacpp",
                "--name",
                "fake-thread",
                "--prefill",
                "user_default",
            )
            run_cli(state, "thread", "append", "--thread", "fake-thread", "--role", "user", "--content", "seed prompt")
            run_cli(state, "checkpoint", "--thread", "fake-thread", "--hard", "--slot", "0", "--capsule-id", "cap_test")
            run_cli(
                state,
                "thread",
                "append",
                "--thread",
                "fake-thread",
                "--role",
                "assistant",
                "--content",
                "new diff after checkpoint",
            )
            run_cli(state, "resume", "--thread", "fake-thread", "--slot", "1", "--append-diff")

            ledger = json.loads((state / "threads" / "fake-thread" / "thread-ledger.json").read_text(encoding="utf-8"))
            manifest = json.loads((state / "threads" / "fake-thread" / "manifests" / "cap_test.json").read_text(encoding="utf-8"))
            if ledger["active_capsule_id"] != "cap_test":
                raise AssertionError("active capsule was not cap_test")
            first_message = json.loads((state / "threads" / "fake-thread" / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
            if first_message["token_start"] <= 0:
                raise AssertionError("thread message did not start after prefill token range")
            if manifest["storage"]["mode"] != "local_file":
                raise AssertionError("hard checkpoint did not use local_file storage")
            snapshot_ref = manifest["storage"].get("snapshot_ref")
            if not snapshot_ref or Path(snapshot_ref).is_absolute() or str(snapshot_ref).replace("\\", "/").startswith(".capsules/"):
                raise AssertionError(f"hard checkpoint did not use a store-relative snapshot_ref: {snapshot_ref}")
            if manifest["context"]["segments"][0]["source"] != "prefill":
                raise AssertionError("hard checkpoint did not preserve parent prefill segment")
            if manifest["storage"]["snapshot_bytes"] != len(b"fake-slot-state"):
                raise AssertionError("snapshot bytes were not recorded from fake save")

            paths = [event["path"] for event in FakeLlamaHandler.events]
            if "/slots/0" not in paths:
                raise AssertionError("slot save request was not observed")
            if "/slots/1" not in paths:
                raise AssertionError("slot restore request was not observed")
            if "/v1/chat/completions" not in paths:
                raise AssertionError("append-diff chat completion was not observed")

            shutdown_job = jobs / "shutdown-thread.json"
            write_job(
                shutdown_job,
                "shutdown_thread",
                {
                    "thread_id": "fake-thread",
                    "slot": 1,
                    "capsule_id": "job_shutdown_cap",
                    "force": True,
                },
            )
            shutdown_result = run_cli(state, "job", "run", str(shutdown_job))
            if "saved shutdown checkpoint: job_shutdown_cap" not in shutdown_result.stdout:
                raise AssertionError("shutdown_thread job did not save the expected checkpoint")
            job_shutdown_manifest = state / "threads" / "fake-thread" / "manifests" / "job_shutdown_cap.json"
            if not job_shutdown_manifest.exists():
                raise AssertionError("shutdown_thread job did not create a manifest")

            run_cli(state, "thread", "start", "--endpoint", "local-llamacpp", "--name", "fallback-thread")
            run_cli(state, "thread", "append", "--thread", "fallback-thread", "--role", "user", "--content", "restore fallback prompt")
            run_cli(state, "checkpoint", "--thread", "fallback-thread", "--hard", "--slot", "0", "--capsule-id", "cap_restore_fail")
            run_cli(
                state,
                "thread",
                "append",
                "--thread",
                "fallback-thread",
                "--role",
                "assistant",
                "--content",
                "diff after failed restore",
            )
            FakeLlamaHandler.fail_restore_once = True
            fallback_resume = run_cli(state, "resume", "--thread", "fallback-thread", "--slot", "1", "--append-diff")
            if "warning: restore failed for cap_restore_fail" not in fallback_resume.stdout:
                raise AssertionError("resume did not report restore failure fallback")
            if "saved fallback checkpoint:" not in fallback_resume.stdout:
                raise AssertionError("resume did not save a replacement checkpoint after replay fallback")

            fallback_ledger = json.loads((state / "threads" / "fallback-thread" / "thread-ledger.json").read_text(encoding="utf-8"))
            failed_link = next(item for item in fallback_ledger["capsules"] if item["capsule_id"] == "cap_restore_fail")
            if failed_link["status"] != "restore_failed":
                raise AssertionError("failed restore capsule was not marked restore_failed")
            if not str(fallback_ledger["active_capsule_id"]).startswith("fallback_"):
                raise AssertionError("fallback replay checkpoint did not become active")
            replay_events = [
                event
                for event in FakeLlamaHandler.events
                if event["path"] == "/v1/chat/completions"
                and event["payload"].get("cache_prompt") is False
                and len(event["payload"].get("messages", [])) == 2
            ]
            if not replay_events:
                raise AssertionError("restore fallback did not replay the canonical transcript with cache_prompt=false")
    finally:
        server.shutdown()
        server.server_close()

    print("fake llama.cpp CLI smoke test ok")


if __name__ == "__main__":
    main()
