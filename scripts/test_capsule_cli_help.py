#!/usr/bin/env python3
"""Smoke test conceptual CLI help topics."""

from __future__ import annotations

import subprocess
import sys
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
    for expected in ["overview", "config", "gateway", "transport", "storage", "state", "security", "model-plane", "troubleshooting"]:
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

    gateway = run_cli("help", "gateway")
    if "http://127.0.0.1:8765/v1" not in gateway:
        raise AssertionError("gateway help did not include client base URL")

    transport = run_cli("help", "transport")
    if "/api/capsules/export" not in transport or "application/vnd.session-capsule.scap" not in transport:
        raise AssertionError("transport help did not include gateway bundle API")

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

    security = run_cli("help", "security")
    if (
        "optional HMAC-SHA256 bundle signatures" not in security
        or "keys are not written into .capsules state" not in security
        or "import warns on local endpoint metadata conflicts" not in security
    ):
        raise AssertionError("security help did not explain integrity boundary")

    troubleshooting = run_cli("help", "troubleshooting")
    if "restore_failed" not in troubleshooting:
        raise AssertionError("troubleshooting help did not include restore failure fallback")

    print("CLI conceptual help smoke test ok")


if __name__ == "__main__":
    main()
