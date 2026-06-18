#!/usr/bin/env python3
"""Smoke test Model Plane job packets for the capsule CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import capsule_gateway


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
        raise AssertionError(
            f"CLI unexpectedly succeeded: {' '.join(command)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
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
        downloaded_bundle = temp_path / "job-thread-gateway.scap"
        gateway_token = temp_path / "gateway-token.txt"
        gateway_token.write_text("job-gateway-token\n", encoding="utf-8")
        gateway_auth_args = ("--gateway-auth-token-file", str(gateway_token))
        signature_key = temp_path / "job-signature.key"
        signature_key.write_text("job-signing-key\n", encoding="utf-8")
        job_signature_args = ("--signature-key-file", str(signature_key), "--signature-key-id", "job-test")

        launch_profile = ROOT / "examples" / "model-plane" / "gateway-launch-profile.example.json"
        launch_command = run_cli(state, "gateway", "command", str(launch_profile), "--json")
        launch_payload = json.loads(launch_command.stdout)
        command_args = launch_payload["command"]
        if "--auth-token-file" not in command_args or ".capsule-gateway-token" not in command_args:
            raise AssertionError("gateway launch profile did not render request auth reference")
        if "--signature-key-file" not in command_args or ".capsule-signing.key" not in command_args:
            raise AssertionError("gateway launch profile did not render signing key reference")
        if "--require-bundle-signature" not in command_args:
            raise AssertionError("gateway launch profile did not render required signature flag")
        if "--cors-allow-origin" not in command_args or "http://127.0.0.1:3000" not in command_args:
            raise AssertionError("gateway launch profile did not render CORS origin")
        if launch_payload["status_url"] != "http://127.0.0.1:8765/api/capsules/status":
            raise AssertionError("gateway launch profile did not expose expected status URL")

        bad_profile = jobs / "bad-gateway-launch-profile.json"
        bad_data = json.loads(launch_profile.read_text(encoding="utf-8"))
        bad_data["security"]["request_auth"]["value"] = "do-not-store-me"
        bad_profile.write_text(json.dumps(bad_data, indent=2) + "\n", encoding="utf-8")
        bad_profile_failure = run_cli_failure(state, "gateway", "command", str(bad_profile))
        if "secret references only" not in bad_profile_failure.stderr:
            raise AssertionError("gateway launch profile secret-value guard did not reject inline secret")

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
        run_cli(state, "job", "run", str(export_job), *job_signature_args)
        if not bundle.exists():
            raise AssertionError("export job did not create bundle")
        signed_job_verify = run_cli(state, "verify", str(bundle), "--signature-key-file", str(signature_key), "--require-signature")
        if "signature: verified" not in signed_job_verify.stdout:
            raise AssertionError("export job did not sign bundle with runner-side key")

        secret_param_job = jobs / "bad-secret-param.json"
        write_job(
            secret_param_job,
            "export_thread",
            {
                "thread_id": "job-thread",
                "out": str(temp_path / "bad-secret.scap"),
                "signature_key_file": str(signature_key),
            },
        )
        secret_param_failure = run_cli_failure(state, "job", "run", str(secret_param_job))
        if "must not contain secret input keys" not in secret_param_failure.stderr:
            raise AssertionError("job packet secret key guard did not reject signature_key_file")

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

        shutdown_job = jobs / "shutdown-thread.json"
        write_job(
            shutdown_job,
            "shutdown_thread",
            {"thread_id": "job-thread", "slot": 0, "force": False},
            dry_run=True,
        )
        shutdown_plan = run_cli(state, "job", "run", str(shutdown_job))
        if "type: shutdown_thread" not in shutdown_plan.stdout:
            raise AssertionError("dry-run shutdown job did not print its plan")

        config = capsule_gateway.GatewayConfig(
            state_dir=state.resolve(),
            endpoint_id="local-soft",
            host="127.0.0.1",
            port=0,
            slot=0,
            checkpoint_mode="soft",
            timeout=20.0,
            default_prefill=None,
            default_thread_prefix="gateway",
            max_bundle_bytes=10 * 1000 * 1000,
            signature_key_file=None,
            signature_key_env=None,
            signature_key_id=None,
            require_bundle_signature=False,
            auth_token="job-gateway-token",
            lock=threading.Lock(),
            cors_allow_origin="http://127.0.0.1:3000",
        )
        gateway = capsule_gateway.create_server(config)
        gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        gateway_thread.start()
        gateway_url = f"http://127.0.0.1:{gateway.server_port}"
        try:
            status_profile = jobs / "gateway-status-profile.json"
            status_profile.write_text(
                json.dumps(
                    {
                        "schema_version": "0.1",
                        "profile_id": "status-check-profile",
                        "profile_type": "session_capsule_gateway",
                        "created_at": "2026-06-18T12:00:00-05:00",
                        "gateway": {
                            "state_dir": str(state),
                            "endpoint_id": "local-soft",
                            "host": "127.0.0.1",
                            "port": gateway.server_port,
                            "checkpoint_mode": "soft",
                            "slot": 0,
                            "timeout_seconds": 20,
                            "max_bundle_bytes": "10000000B",
                            "cors_allow_origin": "http://127.0.0.1:3000",
                        },
                        "transport": {
                            "openai_base_url": f"{gateway_url}/v1",
                            "status_url": f"{gateway_url}/api/capsules/status",
                            "require_status_transport": True,
                        },
                        "security": {
                            "request_auth": {"source": "file", "ref": str(gateway_token)},
                            "bundle_signing": {
                                "source": "none",
                                "ref": None,
                                "key_id": None,
                                "require_on_import": False,
                            },
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            profile_status = run_cli(state, "gateway", "check", str(status_profile), "--json")
            profile_status_payload = json.loads(profile_status.stdout)
            if profile_status_payload.get("transport_verified") is not True:
                raise AssertionError("gateway profile status check did not verify transport")
            if profile_status_payload.get("endpoint_id") != "local-soft":
                raise AssertionError("gateway profile status check did not report expected endpoint")

            gateway_export_job = jobs / "gateway-export.json"
            write_job(
                gateway_export_job,
                "gateway_export_bundle",
                {
                    "gateway_url": gateway_url,
                    "thread_id": "job-thread",
                    "bundle_id": "job-thread-gateway",
                    "include_snapshots": False,
                    "redact_transcript": False,
                    "force": False,
                },
            )
            gateway_export = run_cli(state, "job", "run", str(gateway_export_job), *gateway_auth_args)
            if '"bundle_id": "job-thread-gateway"' not in gateway_export.stdout:
                raise AssertionError("gateway export job did not return expected bundle id")

            gateway_list_job = jobs / "gateway-list.json"
            write_job(gateway_list_job, "gateway_list_bundles", {"gateway_url": gateway_url})
            unauthorized_list = run_cli_failure(state, "job", "run", str(gateway_list_job))
            if "unauthorized" not in unauthorized_list.stderr:
                raise AssertionError("gateway list job without runner auth did not fail with unauthorized")

            gateway_list = run_cli(state, "job", "run", str(gateway_list_job), *gateway_auth_args)
            if "job-thread-gateway" not in gateway_list.stdout:
                raise AssertionError("gateway list job did not include exported bundle")

            gateway_download_job = jobs / "gateway-download.json"
            write_job(
                gateway_download_job,
                "gateway_download_bundle",
                {"gateway_url": gateway_url, "bundle_id": "job-thread-gateway", "out": str(downloaded_bundle)},
            )
            run_cli(state, "job", "run", str(gateway_download_job), *gateway_auth_args)
            if not downloaded_bundle.exists() or not downloaded_bundle.read_bytes().startswith(b"PK"):
                raise AssertionError("gateway download job did not write a .scap bundle")

            gateway_import_upload_job = jobs / "gateway-import-upload.json"
            write_job(
                gateway_import_upload_job,
                "gateway_import_bundle",
                {
                    "gateway_url": gateway_url,
                    "bundle": str(downloaded_bundle),
                    "bundle_id": "uploaded-job-thread",
                    "thread_id": "job-thread-copy",
                    "force": True,
                },
            )
            gateway_import_upload = run_cli(state, "job", "run", str(gateway_import_upload_job), *gateway_auth_args)
            if '"thread_id": "job-thread-copy"' not in gateway_import_upload.stdout:
                raise AssertionError("gateway raw upload import job did not use target thread override")

            gateway_import_stored_job = jobs / "gateway-import-stored.json"
            write_job(
                gateway_import_stored_job,
                "gateway_import_bundle",
                {"gateway_url": gateway_url, "bundle_id": "uploaded-job-thread", "thread_id": "job-thread-stored", "force": True},
            )
            gateway_import_stored = run_cli(state, "job", "run", str(gateway_import_stored_job), *gateway_auth_args)
            if '"thread_id": "job-thread-stored"' not in gateway_import_stored.stdout:
                raise AssertionError("gateway stored import job did not use target thread override")

            gateway_delete_job = jobs / "gateway-delete.json"
            write_job(
                gateway_delete_job,
                "gateway_delete_bundle",
                {"gateway_url": gateway_url, "bundle_id": "job-thread-gateway"},
            )
            gateway_delete = run_cli(state, "job", "run", str(gateway_delete_job), *gateway_auth_args)
            if '"deleted": true' not in gateway_delete.stdout:
                raise AssertionError("gateway delete job did not delete exported bundle")
        finally:
            gateway.shutdown()
            gateway.server_close()

    print("model-plane job packet smoke test ok")


if __name__ == "__main__":
    main()
