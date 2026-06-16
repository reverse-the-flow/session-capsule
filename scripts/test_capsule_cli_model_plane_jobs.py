#!/usr/bin/env python3
"""Smoke test Model Plane job packets for the capsule CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


def write_job(path: Path, job_type: str, params: dict[str, object], dry_run: bool = False) -> None:
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "job_id": path.stem,
        "job_type": job_type,
        "created_at": "2026-06-16T17:45:00-05:00",
        "requested_by": "smoke-test",
        "params": params,
    }
    if dry_run:
        payload["dry_run"] = True
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="session-capsules-jobs-") as temp:
        temp_path = Path(temp)
        state = temp_path / ".capsules"
        jobs = temp_path / "jobs"
        jobs.mkdir()
        bundle = temp_path / "job-thread.scap"

        run_cli(
            state,
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
        run_cli(state, "thread", "start", "--endpoint", "local-soft", "--name", "job-thread")
        run_cli(state, "thread", "append", "--thread", "job-thread", "--role", "user", "--content", "Message before job checkpoint.")

        checkpoint_job = jobs / "checkpoint-soft.json"
        write_job(checkpoint_job, "checkpoint_thread", {"thread_id": "job-thread", "mode": "soft"})
        run_cli(state, "job", "run", str(checkpoint_job))

        ledger = json.loads((state / "threads" / "job-thread" / "thread-ledger.json").read_text(encoding="utf-8"))
        if ledger["active_capsule_id"] is None:
            raise AssertionError("checkpoint job did not create an active capsule")

        validate_job = jobs / "validate-active.json"
        write_job(validate_job, "validate_capsule", {"thread_id": "job-thread", "require_snapshot": False})
        validate_result = run_cli(state, "job", "run", str(validate_job))
        if "compatible: yes" not in validate_result.stdout:
            raise AssertionError("validate job did not report compatibility")

        export_job = jobs / "export-thread.json"
        write_job(
            export_job,
            "export_thread",
            {
                "thread_id": "job-thread",
                "out": str(bundle),
                "include_snapshots": False,
                "redact_transcript": False,
                "force": False,
            },
        )
        run_cli(state, "job", "run", str(export_job))
        if not bundle.exists():
            raise AssertionError("export job did not create bundle")

        resume_job = jobs / "resume-thread.json"
        write_job(
            resume_job,
            "resume_thread",
            {"thread_id": "job-thread", "slot": 0, "append_diff": True},
            dry_run=True,
        )
        resume_result = run_cli(state, "job", "run", str(resume_job))
        if "type: resume_thread" not in resume_result.stdout:
            raise AssertionError("dry-run resume job did not print its plan")

    print("model-plane job packet smoke test ok")


if __name__ == "__main__":
    main()
