#!/usr/bin/env python3
"""Smoke test .scap export/import for the capsule CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "capsule_cli.py"


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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="session-capsules-scap-") as temp:
        temp_path = Path(temp)
        source_state = temp_path / "source" / ".capsules"
        imported_state = temp_path / "imported" / ".capsules"
        prefill_path = temp_path / "prefill.md"
        bundle_path = temp_path / "thread.scap"

        prefill_path.write_text("Stable source-only user prefill.", encoding="utf-8")

        run_cli(
            source_state,
            "endpoint",
            "add",
            "local-soft",
            "--type",
            "hosted",
            "--base-url",
            "http://example.invalid",
            "--model-ref",
            "hosted-model",
            "--context-limit",
            "4096",
        )
        run_cli(source_state, "prefill", "create", "--endpoint", "local-soft", "--name", "user_default", "--input", str(prefill_path), "--soft")
        run_cli(source_state, "thread", "start", "--endpoint", "local-soft", "--prefill", "user_default", "--name", "export-thread")
        run_cli(source_state, "thread", "append", "--thread", "export-thread", "--role", "user", "--content", "First live message.")
        run_cli(source_state, "checkpoint", "--thread", "export-thread", "--soft")
        run_cli(source_state, "export", "--thread", "export-thread", "--out", str(bundle_path))

        if not bundle_path.exists():
            raise AssertionError("bundle was not created")

        with zipfile.ZipFile(bundle_path, "r") as bundle:
            names = set(bundle.namelist())
            required = {
                "manifest.json",
                "thread-ledger.json",
                "transcript.jsonl",
                "capsule-index.json",
                "threads/export-thread/thread-ledger.json",
                "threads/export-thread/transcript.jsonl",
                "prefills/user_default/v001/manifest.json",
                "prefills/user_default/v001/source.md",
                "endpoints/local-soft.json",
            }
            missing = required - names
            if missing:
                raise AssertionError(f"bundle missing entries: {sorted(missing)}")
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            if manifest["includes_snapshots"]:
                raise AssertionError("ledger-only export unexpectedly included snapshots")

        run_cli(imported_state, "import", str(bundle_path))
        imported_ledger = imported_state / "threads" / "export-thread" / "thread-ledger.json"
        imported_transcript = imported_state / "threads" / "export-thread" / "transcript.jsonl"
        if not imported_ledger.exists():
            raise AssertionError("imported ledger missing")
        if not imported_transcript.exists():
            raise AssertionError("imported transcript missing")
        ledger = json.loads(imported_ledger.read_text(encoding="utf-8"))
        if ledger["active_capsule_id"] is None:
            raise AssertionError("imported ledger did not preserve active capsule")
        run_cli(imported_state, "inspect", "--thread", "export-thread")

    print(".scap export/import smoke test ok")


if __name__ == "__main__":
    main()
