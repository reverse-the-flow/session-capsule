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
    for expected in ["overview", "config", "gateway", "storage", "model-plane", "troubleshooting"]:
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

    print("CLI conceptual help smoke test ok")


if __name__ == "__main__":
    main()
