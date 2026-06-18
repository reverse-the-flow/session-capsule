#!/usr/bin/env python3
"""Smoke test conceptual CLI help topics."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "capsule_cli.py"


def run_cli(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed: {' '.join(args)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout


def main() -> None:
    topics = run_cli("help", "--topics")
    for expected in [
        "overview",
        "config",
        "gateway",
        "integrations",
        "transport",
        "storage",
        "state",
        "security",
        "model-plane",
        "troubleshooting",
    ]:
        if expected not in topics:
            raise AssertionError(f"help topic missing: {expected}")

    overview = run_cli("help")
    if "Session Capsules keep the transcript canonical" not in overview:
        raise AssertionError("overview help did not print expected summary")

    storage = run_cli("help", "storage")
    if "Pinned capsules are always protected" not in storage:
        raise AssertionError("storage help did not describe pinned protection")

    state_help = run_cli("help", "state")
    if "project-local" not in state_help or "--state-dir" not in state_help:
        raise AssertionError("state help did not explain project-local default and override")

    state_info = run_cli("state", "info")
    if "policy: project_local_default" not in state_info or "default_state_dir: .capsules" not in state_info:
        raise AssertionError("state info did not report the v0 state location policy")

    endpoint = run_cli("help", "endpoint")
    if "endpoint matrix" not in endpoint or "--json" not in endpoint:
        raise AssertionError("endpoint help did not include slot compatibility matrix command")

    gateway = run_cli("help", "gateway")
    if "http://127.0.0.1:8765/v1" not in gateway:
        raise AssertionError("gateway help did not include client base URL")
    if "X-OpenWebUI-Chat-Id" not in gateway or "X-Opencode-Session" not in gateway:
        raise AssertionError("gateway help did not include native client identity headers")

    integrations = run_cli("help", "integrations")
    if "opencode-config" not in integrations or "X-Capsule-Thread" not in integrations:
        raise AssertionError("integrations help did not include opencode config generation")

    with tempfile.TemporaryDirectory(prefix="session-capsules-opencode-") as temp:
        temp_path = Path(temp)
        out_path = temp_path / "opencode.generated.json"
        payload = json.loads(
            run_cli(
                "integration",
                "opencode-config",
                "--workspace",
                str(temp_path / "repo"),
                "--session",
                "session-42",
                "--prefill",
                "user_default",
                "--gateway-url",
                "http://127.0.0.1:8765",
                "--out",
                str(out_path),
                "--json",
            )
        )
        if payload.get("integration_type") != "opencode_config":
            raise AssertionError("opencode integration command did not report the expected type")
        if not payload.get("thread", "").startswith("opencode-"):
            raise AssertionError("opencode integration command did not derive a thread id")
        generated = json.loads(out_path.read_text(encoding="utf-8"))
        provider = generated["provider"]["session-capsules"]
        options = provider["options"]
        headers = options["headers"]
        if options.get("apiKey") != "{env:CAPSULE_GATEWAY_TOKEN}":
            raise AssertionError("opencode config did not keep gateway token as an environment reference")
        if headers.get("X-Capsule-Thread") != payload["thread"]:
            raise AssertionError("opencode config did not write a concrete capsule thread header")
        if headers.get("X-Capsule-Prefill") != "user_default":
            raise AssertionError("opencode config did not write the selected prefill header")
        if "gateway-token" in out_path.read_text(encoding="utf-8").lower():
            raise AssertionError("opencode config appears to contain an inline gateway token")
        print("opencode integration config generation smoke test ok")

    transport = run_cli("help", "transport")
    if "/api/capsules/export" not in transport or "application/vnd.session-capsule.scap" not in transport:
        raise AssertionError("transport help did not include gateway bundle API")
    if "gateway download" not in transport or "gateway upload" not in transport:
        raise AssertionError("transport help did not include direct gateway upload/download commands")

    model_plane = run_cli("help", "model-plane")
    if "gateway_export_bundle" not in model_plane:
        raise AssertionError("model-plane help did not include gateway transport job types")
    if "shutdown_thread" not in model_plane:
        raise AssertionError("model-plane help did not include lifecycle shutdown job type")
    if "--gateway-auth-token-file" not in model_plane:
        raise AssertionError("model-plane help did not include protected gateway job auth flags")
    if "--signature-key-file" not in model_plane:
        raise AssertionError("model-plane help did not include export job signing flags")
    if "gateway command" not in model_plane:
        raise AssertionError("model-plane help did not include gateway launch-profile command rendering")
    if "gateway check" not in model_plane:
        raise AssertionError("model-plane help did not include gateway launch-profile status checking")
    if "endpoint_verified" not in model_plane:
        raise AssertionError("model-plane help did not include endpoint readiness status")
    if "gateway status" not in model_plane or "gateway upload" not in model_plane:
        raise AssertionError("model-plane help did not include direct gateway transport commands")

    security = run_cli("help", "security")
    if (
        "optional HMAC-SHA256 bundle signatures" not in security
        or "keys are not written into .capsules state" not in security
        or "import warns on local endpoint metadata conflicts" not in security
        or "inspect --bundle" not in security
        or "bundle-policy" not in security
    ):
        raise AssertionError("security help did not explain integrity boundary")

    troubleshooting = run_cli("help", "troubleshooting")
    if "restore_failed" not in troubleshooting:
        raise AssertionError("troubleshooting help did not include restore failure fallback")

    print("CLI conceptual help smoke test ok")


if __name__ == "__main__":
    main()
