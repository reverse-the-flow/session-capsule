#!/usr/bin/env python3
"""Smoke test capsule storage config, pinning, stats, and GC."""

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


class FakeSlotHandler(BaseHTTPRequestHandler):
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
            self.send_json([{"id": 0, "n_ctx": 8192, "is_processing": False}])
            return
        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self.read_payload()
        if parsed.path.startswith("/slots/"):
            action = parse_qs(parsed.query).get("action", [""])[0]
            filename = payload.get("filename")
            if action == "save":
                if filename:
                    path = Path(filename)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fake-gc-slot-state")
                self.send_json({"filename": filename, "n_written": len(b"fake-gc-slot-state")})
                return
        self.send_json({"error": "not found"}, status=404)


def run_cli(state_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLI), "--state-dir", str(state_dir), *args]
    result = subprocess.run(command, cwd=ROOT, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def snapshot_path(state: Path, thread_id: str, capsule_id: str) -> Path:
    manifest = json.loads(
        (state / "threads" / thread_id / "manifests" / f"{capsule_id}.json").read_text(encoding="utf-8")
    )
    return state / manifest["storage"]["snapshot_ref"]


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlotHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with tempfile.TemporaryDirectory(prefix="session-capsules-gc-") as temp:
            state = Path(temp) / ".capsules"
            run_cli(state, "config", "init")
            run_cli(state, "config", "set", "storage.max_bytes", "1B")
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
            run_cli(state, "thread", "start", "--endpoint", "local-llamacpp", "--name", "gc-thread")
            run_cli(state, "thread", "append", "--thread", "gc-thread", "--role", "user", "--content", "old message")
            run_cli(state, "checkpoint", "--thread", "gc-thread", "--hard", "--slot", "0", "--capsule-id", "cap_old")
            run_cli(state, "thread", "append", "--thread", "gc-thread", "--role", "user", "--content", "new message")
            run_cli(state, "checkpoint", "--thread", "gc-thread", "--hard", "--slot", "0", "--capsule-id", "cap_new")

            old_snapshot = snapshot_path(state, "gc-thread", "cap_old")
            new_snapshot = snapshot_path(state, "gc-thread", "cap_new")
            if not old_snapshot.exists() or not new_snapshot.exists():
                raise AssertionError("test setup did not create both hard snapshots")

            run_cli(state, "pin", "--thread", "gc-thread", "--capsule-id", "cap_old")
            pinned_plan = run_cli(state, "gc", "--dry-run")
            if "gc candidates: 0" not in pinned_plan.stdout:
                raise AssertionError("pinned old capsule should not be a GC candidate")

            run_cli(state, "unpin", "--thread", "gc-thread", "--capsule-id", "cap_old")
            plan = run_cli(state, "gc", "--dry-run")
            if "cap_old" not in plan.stdout:
                raise AssertionError("unpinned old capsule should be a GC candidate")
            if "cap_new" in plan.stdout:
                raise AssertionError("latest hard capsule should be protected from GC")

            run_cli(state, "gc", "--apply")
            if old_snapshot.exists():
                raise AssertionError("GC did not delete old unpinned snapshot")
            if not new_snapshot.exists():
                raise AssertionError("GC deleted latest protected snapshot")

            ledger = json.loads((state / "threads" / "gc-thread" / "thread-ledger.json").read_text(encoding="utf-8"))
            old_link = next(item for item in ledger["capsules"] if item["capsule_id"] == "cap_old")
            if old_link["status"] != "missing":
                raise AssertionError("GC did not mark deleted snapshot link as missing")

            stats = run_cli(state, "stats")
            if "storage.max_bytes: 1B" not in stats.stdout:
                raise AssertionError("stats did not read persisted storage config")
    finally:
        server.shutdown()
        server.server_close()

    print("storage config and GC smoke test ok")


if __name__ == "__main__":
    main()
