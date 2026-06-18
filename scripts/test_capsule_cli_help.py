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
    for expected in ["overview", "config", "gateway", "transport", "storage", "security", "model-plane", "troubleshooting"]:
        if expected not in topics:
            raise AssertionError(f"help topic missing: {expected}")

    overview = run_cli("help")
    if "Session Capsules keep the transcript canonical" not in overview:
        raise AssertionError("overview help did not print expected summary")

    storage = run_cli("help", "storage")
    if "Pinned capsules are always protected" not in storage:
        raise AssertionError("storage help did not describe pinned protection")

    gateway = run_cli("help", "gateway")
    if "http://127.0.0.1:8765/v1" not in gateway:
        raise AssertionError("gateway help did not include client base URL")

    transport = run_cli("help", "transport")
    if "/api/capsules/export" not in transport or "application/vnd.session-capsule.scap" not in transport:
        raise AssertionError("transport help did not include gateway bundle API")

    model_plane = run_cli("help", "model-plane")
    if "gateway_export_bundle" not in model_plane:
        raise AssertionError("model-plane help did not include gateway transport job types")

    security = run_cli("help", "security")
    if "optional HMAC-SHA256 bundle signatures" not in security or "keys are not written into .capsules state" not in security:
        raise AssertionError("security help did not explain integrity boundary")

    print("CLI conceptual help smoke test ok")


if __name__ == "__main__":
    main()
