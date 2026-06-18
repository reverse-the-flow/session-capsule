#!/usr/bin/env python3
"""Smoke test .scap export/import for the capsule CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import warnings
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


def run_cli_failure(state_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CLI), "--state-dir", str(state_dir), *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        raise AssertionError(f"CLI unexpectedly succeeded: {' '.join(command)}\nSTDOUT:\n{result.stdout}")
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="session-capsules-scap-") as temp:
        temp_path = Path(temp)
        source_state = temp_path / "source" / ".capsules"
        imported_state = temp_path / "imported" / ".capsules"
        signed_import_state = temp_path / "signed-imported" / ".capsules"
        prefill_path = temp_path / "prefill.md"
        bundle_path = temp_path / "thread.scap"
        signed_bundle_path = temp_path / "thread-signed.scap"
        signature_key_path = temp_path / "signature.key"

        prefill_path.write_text("Stable source-only user prefill.", encoding="utf-8")
        signature_key_path.write_text("test-local-signing-key", encoding="utf-8")

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
            file_digests = manifest.get("file_digests", {})
            if "thread-ledger.json" not in file_digests:
                raise AssertionError("bundle manifest did not include file digest index")
            if manifest.get("integrity", {}).get("file_digest_algorithm") != "sha256":
                raise AssertionError("bundle manifest did not record sha256 integrity metadata")

        verify_result = run_cli(source_state, "verify", str(bundle_path))
        if "verified: yes" not in verify_result.stdout:
            raise AssertionError("bundle verify command did not accept exported bundle")

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

        tampered_bundle = temp_path / "tampered.scap"
        tampered_bundle.write_bytes(bundle_path.read_bytes())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(tampered_bundle, "a", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("thread-ledger.json", "{}\n")
        verify_failure = run_cli_failure(source_state, "verify", str(tampered_bundle))
        if "Duplicate bundle entry" not in verify_failure.stderr:
            raise AssertionError("tampered bundle did not fail duplicate-entry verification")
        import_failure = run_cli_failure(imported_state, "import", str(tampered_bundle), "--force")
        if "Duplicate bundle entry" not in import_failure.stderr:
            raise AssertionError("tampered bundle import did not fail integrity verification")

        mismatched_bundle = temp_path / "mismatched.scap"
        with zipfile.ZipFile(bundle_path, "r") as source, zipfile.ZipFile(mismatched_bundle, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                payload = source.read(item.filename)
                if item.filename == "thread-ledger.json":
                    payload = b"{}\n"
                target.writestr(item, payload)
        mismatch_failure = run_cli_failure(source_state, "verify", str(mismatched_bundle))
        if "mismatched" not in mismatch_failure.stderr:
            raise AssertionError("mismatched bundle did not fail digest verification")

        unsigned_signature_failure = run_cli_failure(source_state, "verify", str(bundle_path), "--require-signature", "--signature-key-file", str(signature_key_path))
        if "Bundle signature is not present" not in unsigned_signature_failure.stderr:
            raise AssertionError("unsigned bundle did not fail required signature verification")

        run_cli(
            source_state,
            "export",
            "--thread",
            "export-thread",
            "--out",
            str(signed_bundle_path),
            "--signature-key-file",
            str(signature_key_path),
            "--signature-key-id",
            "test-key",
        )
        signed_verify = run_cli(
            source_state,
            "verify",
            str(signed_bundle_path),
            "--signature-key-file",
            str(signature_key_path),
            "--require-signature",
        )
        if "signature: verified" not in signed_verify.stdout:
            raise AssertionError("signed bundle did not verify with the expected key")
        wrong_key_path = temp_path / "wrong-signature.key"
        wrong_key_path.write_text("wrong-key", encoding="utf-8")
        wrong_key_failure = run_cli_failure(
            source_state,
            "verify",
            str(signed_bundle_path),
            "--signature-key-file",
            str(wrong_key_path),
            "--require-signature",
        )
        if "Bundle signature verification failed" not in wrong_key_failure.stderr:
            raise AssertionError("signed bundle did not reject the wrong signature key")

        run_cli(
            signed_import_state,
            "import",
            str(signed_bundle_path),
            "--signature-key-file",
            str(signature_key_path),
            "--require-signature",
        )
        signed_imported_ledger = signed_import_state / "threads" / "export-thread" / "thread-ledger.json"
        if not signed_imported_ledger.exists():
            raise AssertionError("signed import did not create thread ledger")

    print(".scap export/import smoke test ok")


if __name__ == "__main__":
    main()
