#!/usr/bin/env python3
"""Smoke test the local capsule gateway against a fake OpenAI/llama.cpp backend."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import parse_qs, urlparse

import capsule_gateway


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "capsule_cli.py"


class FakeBackendHandler(BaseHTTPRequestHandler):
    events: list[dict[str, Any]] = []
    completion_count = 0

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
        if self.path == "/v1/models":
            self.send_json({"data": [{"id": "fake-model", "object": "model"}]})
            return
        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self.read_payload()
        FakeBackendHandler.events.append(
            {"path": parsed.path, "query": parsed.query, "payload": payload}
        )

        if parsed.path.startswith("/slots/"):
            action = parse_qs(parsed.query).get("action", [""])[0]
            filename = payload.get("filename")
            if action == "save":
                if filename:
                    path = Path(filename)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fake-gateway-slot")
                self.send_json({"filename": filename, "n_written": len(b"fake-gateway-slot")})
                return
            if action == "restore":
                self.send_json({"filename": filename, "timings": {"restore_ms": 0.5}})
                return

        if parsed.path == "/v1/chat/completions":
            FakeBackendHandler.completion_count += 1
            self.send_json(
                {
                    "id": f"chatcmpl-{FakeBackendHandler.completion_count}",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": f"gateway response {FakeBackendHandler.completion_count}",
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": len(payload.get("messages", [])), "completion_tokens": 3},
                }
            )
            return

        self.send_json({"error": "not found"}, status=404)


def run_cli(state_dir: Path, *args: str) -> None:
    command = [sys.executable, str(CLI), "--state-dir", str(state_dir), *args]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
        return body, {key: value for key, value in response.headers.items()}


def main() -> None:
    FakeBackendHandler.events = []
    FakeBackendHandler.completion_count = 0
    backend = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackendHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    backend_url = f"http://127.0.0.1:{backend.server_port}"

    gateway = None
    try:
        with tempfile.TemporaryDirectory(prefix="session-capsules-gateway-") as temp:
            state = Path(temp) / ".capsules"
            prefill_path = Path(temp) / "prefill.md"
            prefill_path.write_text("Stable gateway prefill.", encoding="utf-8")

            run_cli(
                state,
                "endpoint",
                "add",
                "local-llamacpp",
                "--type",
                "llamacpp",
                "--base-url",
                backend_url,
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
            run_cli(state, "prefill", "create", "--endpoint", "local-llamacpp", "--name", "user_default", "--input", str(prefill_path), "--soft")

            config = capsule_gateway.GatewayConfig(
                state_dir=state.resolve(),
                endpoint_id="local-llamacpp",
                host="127.0.0.1",
                port=0,
                slot=0,
                checkpoint_mode="hard",
                timeout=20.0,
                default_prefill=None,
                default_thread_prefix="gateway",
                lock=threading.Lock(),
            )
            gateway = capsule_gateway.create_server(config)
            gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            gateway_thread.start()
            gateway_url = f"http://127.0.0.1:{gateway.server_port}"

            first_payload = {
                "model": "fake-model",
                "stream": False,
                "messages": [{"role": "user", "content": "First request"}],
            }
            response, headers = post_json(
                f"{gateway_url}/v1/chat/completions",
                first_payload,
                {"X-Capsule-Thread": "gateway-thread", "X-Capsule-Prefill": "user_default"},
            )
            if response["choices"][0]["message"]["content"] != "gateway response 1":
                raise AssertionError("unexpected first gateway response")
            if headers.get("X-Capsule-Thread") != "gateway-thread":
                raise AssertionError("gateway did not echo thread header")

            second_payload = {
                "model": "fake-model",
                "stream": False,
                "messages": [
                    {"role": "user", "content": "First request"},
                    {"role": "assistant", "content": "gateway response 1"},
                    {"role": "user", "content": "Second request"},
                ],
            }
            response, headers = post_json(
                f"{gateway_url}/v1/chat/completions",
                second_payload,
                {"X-Capsule-Thread": "gateway-thread"},
            )
            if response["choices"][0]["message"]["content"] != "gateway response 2":
                raise AssertionError("unexpected second gateway response")
            if headers.get("X-Capsule-Mode") != "restore":
                raise AssertionError("second request did not restore a hard capsule")

            slot_events = [event for event in FakeBackendHandler.events if event["path"].startswith("/slots/")]
            chat_events = [event for event in FakeBackendHandler.events if event["path"] == "/v1/chat/completions"]
            if not any("action=restore" in event["query"] for event in slot_events):
                raise AssertionError("gateway did not issue slot restore")
            if len(chat_events) < 2:
                raise AssertionError("expected two backend chat events")
            second_backend_messages = chat_events[-1]["payload"]["messages"]
            if second_backend_messages != [{"role": "user", "content": "Second request"}]:
                raise AssertionError(f"gateway did not forward only diff messages: {second_backend_messages}")

            ledger = json.loads((state / "threads" / "gateway-thread" / "thread-ledger.json").read_text(encoding="utf-8"))
            if ledger["active_capsule_id"] is None:
                raise AssertionError("gateway did not checkpoint the thread")
            if len(ledger["capsules"]) < 3:
                raise AssertionError("expected prefill plus hard checkpoints in ledger")

            open_webui_payload = {
                "model": "fake-model",
                "stream": False,
                "messages": [{"role": "user", "content": "Open WebUI request"}],
            }
            response, headers = post_json(
                f"{gateway_url}/v1/chat/completions",
                open_webui_payload,
                {
                    "X-OpenWebUI-Chat-Id": "open-webui-chat-42",
                    "X-OpenWebUI-User-Id": "user-alpha",
                },
            )
            if response["choices"][0]["message"]["content"] != "gateway response 3":
                raise AssertionError("unexpected Open WebUI gateway response")
            if headers.get("X-Capsule-Thread") != "open-webui-chat-42":
                raise AssertionError("gateway did not derive thread from Open WebUI chat id")
            open_webui_ledger = json.loads(
                (state / "threads" / "open-webui-chat-42" / "thread-ledger.json").read_text(encoding="utf-8")
            )
            if open_webui_ledger.get("workspace_ref") != "user-alpha":
                raise AssertionError("gateway did not derive workspace from Open WebUI user id")
    finally:
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        backend.shutdown()
        backend.server_close()

    print("capsule gateway fake backend smoke test ok")


if __name__ == "__main__":
    main()
