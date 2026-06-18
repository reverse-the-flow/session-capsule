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
        renamed_state = temp_path / "renamed" / ".capsules"
        conflict_state = temp_path / "conflict" / ".capsules"
        signed_import_state = temp_path / "signed-imported" / ".capsules"
        redacted_import_state = temp_path / "redacted-imported" / ".capsules"
        prefill_path = temp_path / "prefill.md"
        bundle_path = temp_path / "thread.scap"
        redacted_bundle_path = temp_path / "thread-redacted.scap"
        signed_bundle_path = temp_path / "thread-signed.scap"
        sealed_bundle_path = temp_path / "thread-sealed.scap"
        unsealed_bundle_path = temp_path / "thread-unsealed.scap"
        signature_key_path = temp_path / "signature.key"
        age_recipient_path = temp_path / "age-recipient.txt"
        age_identity_path = temp_path / "age-identity.txt"
        fake_age_path = temp_path / "fake-age.cmd"

        prefill_path.write_text("Stable source-only user prefill.", encoding="utf-8")
        signature_key_path.write_text("test-local-signing-key", encoding="utf-8")
        age_recipient_path.write_text("age1testrecipient\n", encoding="utf-8")
        age_identity_path.write_text("AGE-SECRET-KEY-test\n", encoding="utf-8")
        fake_age_path.write_text(
            "\n".join(
                [
                    "@echo off",
                    "setlocal",
                    "set \"OUT=\"",
                    "set \"INPUT=\"",
                    ":loop",
                    "if \"%~1\"==\"\" goto done",
                    "if \"%~1\"==\"-o\" (",
                    "  set \"OUT=%~2\"",
                    "  shift",
                    "  shift",
                    "  goto loop",
                    ")",
                    "if \"%~1\"==\"-r\" (",
                    "  shift",
                    "  shift",
                    "  goto loop",
                    ")",
                    "if \"%~1\"==\"-i\" (",
                    "  shift",
                    "  shift",
                    "  goto loop",
                    ")",
                    "if \"%~1\"==\"-d\" (",
                    "  shift",
                    "  goto loop",
                    ")",
                    "set \"INPUT=%~1\"",
                    "shift",
                    "goto loop",
                    ":done",
                    "if \"%OUT%\"==\"\" exit /b 2",
                    "if \"%INPUT%\"==\"\" exit /b 3",
                    "copy /Y \"%INPUT%\" \"%OUT%\" >nul",
                    "exit /b %ERRORLEVEL%",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

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
        dry_run_result = run_cli(source_state, "export", "--thread", "export-thread", "--out", str(bundle_path), "--dry-run")
        if "would export bundle" not in dry_run_result.stdout or "estimated payload bytes" not in dry_run_result.stdout:
            raise AssertionError("export dry-run did not print a bundle size plan")
        if bundle_path.exists():
            raise AssertionError("export dry-run unexpectedly created a bundle")
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
        inspect_result = run_cli(source_state, "inspect", "--bundle", str(bundle_path), "--json")
        inspect_payload = json.loads(inspect_result.stdout)
        if inspect_payload.get("share_policy", {}).get("classification") != "contains_plaintext_content":
            raise AssertionError("bundle inspect did not classify transcript-bearing export as plaintext content")
        if inspect_payload.get("share_policy", {}).get("trusted_transport_required") is not True:
            raise AssertionError("bundle inspect did not require trusted transport for unencrypted bundle")
        if inspect_payload.get("content", {}).get("transcript_included") is not True:
            raise AssertionError("bundle inspect did not detect transcript content")
        if inspect_payload.get("content", {}).get("prefill_sources_included") is not True:
            raise AssertionError("bundle inspect did not detect prefill source content")
        human_inspect = run_cli(source_state, "inspect", "--bundle", str(bundle_path))
        if "classification: contains_plaintext_content" not in human_inspect.stdout:
            raise AssertionError("human bundle inspect did not print share classification")
        plaintext_policy_failure = run_cli_failure(source_state, "bundle-policy", str(bundle_path), "--preset", "metadata-only")
        if "plaintext transcript or prefill source content is present" not in plaintext_policy_failure.stdout:
            raise AssertionError("bundle policy did not reject plaintext content")
        unsigned_policy_failure = run_cli_failure(source_state, "bundle-policy", str(bundle_path), "--require-signature")
        if "bundle signature is absent" not in unsigned_policy_failure.stdout:
            raise AssertionError("bundle policy did not reject unsigned bundle")

        run_cli(
            source_state,
            "seal",
            str(bundle_path),
            "--out",
            str(sealed_bundle_path),
            "--age-recipient-file",
            str(age_recipient_path),
            "--age-bin",
            str(fake_age_path),
        )
        sealed_inspect_payload = json.loads(run_cli(source_state, "inspect", "--bundle", str(sealed_bundle_path), "--json").stdout)
        if sealed_inspect_payload.get("share_policy", {}).get("classification") != "encrypted":
            raise AssertionError("sealed bundle did not inspect as encrypted")
        if sealed_inspect_payload.get("share_policy", {}).get("trusted_transport_required") is not False:
            raise AssertionError("sealed bundle still required trusted transport")
        if sealed_inspect_payload.get("integrity", {}).get("encryption", {}).get("backend") != "age":
            raise AssertionError("sealed bundle did not record age backend metadata")
        encryption = sealed_inspect_payload.get("integrity", {}).get("encryption", {})
        if encryption.get("recipient_source") != "file":
            raise AssertionError("sealed bundle did not record recipient file source")
        if encryption.get("recipient") != "age1testrecipient":
            raise AssertionError("sealed bundle did not record recipient from recipient file")
        sealed_policy = run_cli(source_state, "bundle-policy", str(sealed_bundle_path), "--preset", "sealed")
        if "policy passed: yes" not in sealed_policy.stdout:
            raise AssertionError("sealed bundle did not pass sealed policy")
        sealed_import_failure = run_cli_failure(source_state, "import", str(sealed_bundle_path))
        if "Sealed bundles must be unsealed before import" not in sealed_import_failure.stderr:
            raise AssertionError("sealed bundle import did not require explicit unseal")
        run_cli(
            source_state,
            "unseal",
            str(sealed_bundle_path),
            "--out",
            str(unsealed_bundle_path),
            "--age-identity",
            str(age_identity_path),
            "--age-bin",
            str(fake_age_path),
        )
        if unsealed_bundle_path.read_bytes() != bundle_path.read_bytes():
            raise AssertionError("unsealed bundle did not match original bundle bytes")
        unsealed_verify = run_cli(source_state, "verify", str(unsealed_bundle_path))
        if "verified: yes" not in unsealed_verify.stdout:
            raise AssertionError("unsealed bundle did not verify")

        run_cli(source_state, "export", "--thread", "export-thread", "--out", str(redacted_bundle_path), "--redact-transcript")
        with zipfile.ZipFile(redacted_bundle_path, "r") as bundle:
            names = set(bundle.namelist())
            if "prefills/user_default/v001/source.md" in names:
                raise AssertionError("redacted bundle unexpectedly included prefill source")
            if bundle.read("transcript.jsonl") or bundle.read("threads/export-thread/transcript.jsonl"):
                raise AssertionError("redacted bundle unexpectedly included transcript content")
            redacted_manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            if redacted_manifest.get("redaction", {}).get("policy") != "metadata_only":
                raise AssertionError("redacted bundle did not record metadata-only redaction policy")
            redacted_ledger = json.loads(bundle.read("thread-ledger.json").decode("utf-8"))
            if redacted_ledger.get("transcript_redacted") is not True:
                raise AssertionError("redacted bundle ledger did not mark transcript_redacted")
            if redacted_ledger.get("fallback", {}).get("mode") != "unavailable_redacted_transcript":
                raise AssertionError("redacted bundle did not disable transcript replay fallback")
            redacted_prefill_manifest = json.loads(bundle.read("prefills/user_default/v001/manifest.json").decode("utf-8"))
            prefill_source = redacted_prefill_manifest.get("prefill_source", {})
            if prefill_source.get("source_ref") is not None or prefill_source.get("source_redacted") is not True:
                raise AssertionError("redacted prefill manifest did not mark omitted source")
        redacted_inspect_payload = json.loads(run_cli(source_state, "inspect", "--bundle", str(redacted_bundle_path), "--json").stdout)
        if redacted_inspect_payload.get("share_policy", {}).get("classification") != "metadata_only_not_encrypted":
            raise AssertionError("bundle inspect did not classify redacted export as metadata-only")
        if redacted_inspect_payload.get("content", {}).get("transcript_included") is not False:
            raise AssertionError("bundle inspect did not detect redacted transcript omission")
        if redacted_inspect_payload.get("content", {}).get("prefill_sources_included") is not False:
            raise AssertionError("bundle inspect did not detect redacted prefill source omission")
        redacted_policy = run_cli(source_state, "bundle-policy", str(redacted_bundle_path), "--preset", "metadata-only", "--json")
        redacted_policy_payload = json.loads(redacted_policy.stdout)
        if redacted_policy_payload.get("passed") is not True:
            raise AssertionError("bundle policy did not accept redacted metadata-only bundle")
        redacted_import = run_cli(redacted_import_state, "import", str(redacted_bundle_path))
        if "warning: transcript was redacted in this bundle" not in redacted_import.stdout:
            raise AssertionError("redacted import did not warn about missing transcript")
        imported_redacted_ledger = json.loads(
            (redacted_import_state / "threads" / "export-thread" / "thread-ledger.json").read_text(encoding="utf-8")
        )
        if imported_redacted_ledger.get("fallback", {}).get("mode") != "unavailable_redacted_transcript":
            raise AssertionError("redacted import did not preserve unavailable fallback")
        redacted_inspect = run_cli(redacted_import_state, "inspect", "--thread", "export-thread")
        if "transcript redacted: yes" not in redacted_inspect.stdout:
            raise AssertionError("inspect did not expose redacted transcript state")

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

        run_cli(renamed_state, "import", str(bundle_path), "--thread-id", "imported-copy")
        renamed_ledger_path = renamed_state / "threads" / "imported-copy" / "thread-ledger.json"
        renamed_transcript_path = renamed_state / "threads" / "imported-copy" / "transcript.jsonl"
        if not renamed_ledger_path.exists():
            raise AssertionError("renamed import did not create remapped ledger")
        if not renamed_transcript_path.exists():
            raise AssertionError("renamed import did not create remapped transcript")
        renamed_ledger = json.loads(renamed_ledger_path.read_text(encoding="utf-8"))
        if renamed_ledger["thread_id"] != "imported-copy":
            raise AssertionError("renamed import did not rewrite ledger thread_id")
        if renamed_ledger["transcript_ref"] != "threads/imported-copy/transcript.jsonl":
            raise AssertionError("renamed import did not rewrite ledger transcript_ref")
        thread_manifest_refs = [
            item["manifest_ref"]
            for item in renamed_ledger["capsules"]
            if str(item["manifest_ref"]).startswith("threads/")
        ]
        if not thread_manifest_refs or not all(ref.startswith("threads/imported-copy/") for ref in thread_manifest_refs):
            raise AssertionError(f"renamed import did not rewrite thread manifest refs: {thread_manifest_refs}")
        renamed_manifest = json.loads((renamed_state / thread_manifest_refs[-1]).read_text(encoding="utf-8"))
        if renamed_manifest["thread_id"] != "imported-copy":
            raise AssertionError("renamed import did not rewrite capsule manifest thread_id")
        prefill_refs = [
            item["manifest_ref"]
            for item in renamed_ledger["capsules"]
            if str(item["manifest_ref"]).startswith("prefills/")
        ]
        if prefill_refs != ["prefills/user_default/v001/manifest.json"]:
            raise AssertionError("renamed import should keep prefill refs state-global")

        run_cli(
            conflict_state,
            "endpoint",
            "add",
            "local-soft",
            "--type",
            "hosted",
            "--base-url",
            "http://other.invalid",
            "--model-ref",
            "other-hosted-model",
            "--model-hash",
            "sha256-other-model",
            "--tokenizer-hash",
            "sha256-other-tokenizer",
            "--context-limit",
            "1024",
        )
        conflict_import = run_cli(conflict_state, "import", str(bundle_path))
        if "warning: endpoint local-soft differs from local endpoint" not in conflict_import.stdout:
            raise AssertionError("import did not warn about endpoint compatibility mismatch")

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
        signed_inspect_payload = json.loads(run_cli(source_state, "inspect", "--bundle", str(signed_bundle_path), "--json").stdout)
        if signed_inspect_payload.get("integrity", {}).get("signature_present") is not True:
            raise AssertionError("bundle inspect did not detect signed bundle")
        if signed_inspect_payload.get("integrity", {}).get("signature_key_id") != "test-key":
            raise AssertionError("bundle inspect did not report signature key id")
        signed_policy = run_cli(source_state, "bundle-policy", str(signed_bundle_path), "--require-signature")
        if "policy passed: yes" not in signed_policy.stdout:
            raise AssertionError("bundle policy did not accept signed bundle with signature requirement")
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
