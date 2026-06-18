#!/usr/bin/env python3
"""Validate the bundled Session Capsules schema examples.

This is intentionally dependency-free. It is not a complete JSON Schema
implementation; it catches the invariants the project relies on before the CLI
adds a full schema validator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"


class ValidationError(Exception):
    """Raised when an example violates a project invariant."""


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return data


def require_keys(name: str, data: dict[str, Any], keys: list[str]) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValidationError(f"{name} is missing required keys: {', '.join(missing)}")


def require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")


def validate_schema_files() -> None:
    expected = [
        "capsule-manifest.schema.json",
        "capsule-config.schema.json",
        "thread-ledger.schema.json",
        "endpoint-capabilities.schema.json",
        "model-plane-job.schema.json",
        "model-plane-gateway-launch.schema.json",
    ]
    for filename in expected:
        schema = load_json(SCHEMAS / filename)
        require_keys(filename, schema, ["$schema", "title", "type", "required", "properties"])
        if schema["type"] != "object":
            raise ValidationError(f"{filename} root type must be object")

    thread_schema = load_json(SCHEMAS / "thread-ledger.schema.json")
    status_enum = (
        thread_schema.get("$defs", {})
        .get("capsule_link", {})
        .get("properties", {})
        .get("status", {})
        .get("enum", [])
    )
    if "restore_failed" not in status_enum:
        raise ValidationError("thread-ledger schema must allow restore_failed capsule links")
    link_props = thread_schema.get("$defs", {}).get("capsule_link", {}).get("properties", {})
    if "last_restore_failed_at" not in link_props:
        raise ValidationError("thread-ledger schema must record last_restore_failed_at on capsule links")

    manifest_schema = load_json(SCHEMAS / "capsule-manifest.schema.json")
    lifecycle_props = manifest_schema.get("properties", {}).get("lifecycle", {}).get("properties", {})
    for key in ["last_restore_failed_at", "last_restore_error"]:
        if key not in lifecycle_props:
            raise ValidationError(f"capsule-manifest schema lifecycle must include {key}")


def validate_endpoint(data: dict[str, Any]) -> None:
    require_keys(
        "endpoint",
        data,
        ["schema_version", "endpoint_id", "type", "base_url", "runtime", "capabilities", "checked_at"],
    )
    if data["schema_version"] != "0.1":
        raise ValidationError("endpoint schema_version must be 0.1")

    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise ValidationError("endpoint.runtime must be an object")
    require_keys(
        "endpoint.runtime",
        runtime,
        ["name", "build", "model_ref", "model_hash", "tokenizer_hash", "context_limit"],
    )
    if not isinstance(runtime["context_limit"], int) or runtime["context_limit"] <= 0:
        raise ValidationError("endpoint.runtime.context_limit must be a positive integer")

    capabilities = data["capabilities"]
    if not isinstance(capabilities, dict):
        raise ValidationError("endpoint.capabilities must be an object")
    for key in [
        "soft_capsules",
        "server_side_handles",
        "slot_save_restore",
        "user_carried_blobs",
        "sealed_blobs",
        "transcript_replay_fallback",
    ]:
        if key not in capabilities:
            raise ValidationError(f"endpoint.capabilities is missing {key}")
        require_bool(f"endpoint.capabilities.{key}", capabilities[key])


def validate_capsule(data: dict[str, Any], endpoint: dict[str, Any]) -> None:
    require_keys(
        "capsule",
        data,
        [
            "schema_version",
            "capsule_id",
            "thread_id",
            "kind",
            "endpoint_id",
            "compatibility",
            "context",
            "storage",
            "created_at",
        ],
    )
    if data["schema_version"] != "0.1":
        raise ValidationError("capsule schema_version must be 0.1")
    if data["endpoint_id"] != endpoint["endpoint_id"]:
        raise ValidationError("capsule endpoint_id must match endpoint example")

    compatibility = data["compatibility"]
    runtime = endpoint["runtime"]
    if compatibility["model_hash"] != runtime["model_hash"]:
        raise ValidationError("capsule model_hash must match endpoint runtime")
    if compatibility["tokenizer_hash"] != runtime["tokenizer_hash"]:
        raise ValidationError("capsule tokenizer_hash must match endpoint runtime")
    if compatibility["context_limit"] != runtime["context_limit"]:
        raise ValidationError("capsule context_limit must match endpoint runtime")

    context = data["context"]
    require_keys("capsule.context", context, ["token_start", "token_end", "token_count", "segments"])
    if context["token_end"] < context["token_start"]:
        raise ValidationError("capsule context token_end must be >= token_start")
    if context["token_count"] != context["token_end"] - context["token_start"]:
        raise ValidationError("capsule token_count must equal token_end - token_start")
    if context["token_end"] > compatibility["context_limit"]:
        raise ValidationError("capsule token_end exceeds compatibility context_limit")

    previous_end = context["token_start"]
    for segment in context["segments"]:
        require_keys("capsule.context.segment", segment, ["segment_id", "source", "role", "token_start", "token_end"])
        if segment["token_start"] != previous_end:
            raise ValidationError("capsule segments must be contiguous")
        if segment["token_end"] < segment["token_start"]:
            raise ValidationError("capsule segment token_end must be >= token_start")
        previous_end = segment["token_end"]
    if previous_end != context["token_end"]:
        raise ValidationError("capsule segments must end at context.token_end")


def validate_thread(data: dict[str, Any], capsule: dict[str, Any], endpoint: dict[str, Any]) -> None:
    require_keys(
        "thread",
        data,
        [
            "schema_version",
            "thread_id",
            "created_at",
            "updated_at",
            "endpoint_id",
            "transcript_ref",
            "active_capsule_id",
            "capsules",
            "fallback",
        ],
    )
    if data["schema_version"] != "0.1":
        raise ValidationError("thread schema_version must be 0.1")
    if data["endpoint_id"] != endpoint["endpoint_id"]:
        raise ValidationError("thread endpoint_id must match endpoint example")
    if data["thread_id"] != capsule["thread_id"]:
        raise ValidationError("thread_id must match capsule example")

    capsule_ids = {item["capsule_id"] for item in data["capsules"]}
    active = data["active_capsule_id"]
    if active not in capsule_ids:
        raise ValidationError("thread active_capsule_id must exist in capsules")
    if active != capsule["capsule_id"]:
        raise ValidationError("thread active capsule must match capsule example")

    by_id = {item["capsule_id"]: item for item in data["capsules"]}
    for item in data["capsules"]:
        parent = item["parent_capsule_id"]
        if parent is not None and parent not in by_id:
            raise ValidationError(f"capsule {item['capsule_id']} references missing parent {parent}")
        if item["token_end"] < item["token_start"]:
            raise ValidationError(f"capsule {item['capsule_id']} has invalid token range")

    fallback = data["fallback"]
    require_keys("thread.fallback", fallback, ["mode", "replay_start_token", "reason"])
    active_link = by_id[active]
    if fallback["replay_start_token"] < active_link["token_end"]:
        raise ValidationError("fallback replay_start_token must not precede active capsule token_end")


def validate_config(data: dict[str, Any]) -> None:
    require_keys("config", data, ["schema_version", "storage"])
    if data["schema_version"] != "0.1":
        raise ValidationError("config schema_version must be 0.1")
    storage = data["storage"]
    if not isinstance(storage, dict):
        raise ValidationError("config.storage must be an object")
    require_keys(
        "config.storage",
        storage,
        [
            "max_bytes",
            "min_free_bytes",
            "prune_policy",
            "keep_latest_per_thread",
            "protect_active_prefills",
        ],
    )
    for key in ["max_bytes", "min_free_bytes"]:
        if not isinstance(storage[key], str) or not re.fullmatch(r"\d+(\.\d+)?\s*([KMGT]i?B|[KMGT]B|B)?", storage[key]):
            raise ValidationError(f"config.storage.{key} must be a byte size string")
    if storage["prune_policy"] != "oldest_unpinned_first":
        raise ValidationError("config.storage.prune_policy must be oldest_unpinned_first")
    if not isinstance(storage["keep_latest_per_thread"], int) or storage["keep_latest_per_thread"] < 0:
        raise ValidationError("config.storage.keep_latest_per_thread must be a nonnegative integer")
    require_bool("config.storage.protect_active_prefills", storage["protect_active_prefills"])


def require_nonnegative_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a nonnegative integer")


def require_nonnegative_number(name: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or value < 0:
        raise ValidationError(f"{name} must be a nonnegative number")


def validate_model_plane_job(path: Path) -> None:
    data = load_json(path)
    name = path.name
    require_keys(name, data, ["schema_version", "job_id", "job_type", "created_at", "params"])
    if data["schema_version"] != "0.1":
        raise ValidationError(f"{name} schema_version must be 0.1")
    job_type = data["job_type"]
    supported = {
        "resume_thread",
        "checkpoint_thread",
        "shutdown_thread",
        "export_thread",
        "validate_capsule",
        "gateway_export_bundle",
        "gateway_list_bundles",
        "gateway_download_bundle",
        "gateway_import_bundle",
        "gateway_delete_bundle",
    }
    if job_type not in supported:
        raise ValidationError(f"{name} has unsupported job_type {job_type}")
    params = data["params"]
    if not isinstance(params, dict):
        raise ValidationError(f"{name} params must be an object")

    if job_type == "resume_thread":
        require_keys(name, params, ["thread_id"])
        if "slot" in params:
            require_nonnegative_int(f"{name}.params.slot", params["slot"])
        if "append_diff" in params:
            require_bool(f"{name}.params.append_diff", params["append_diff"])
        if "max_tokens" in params:
            require_nonnegative_int(f"{name}.params.max_tokens", params["max_tokens"])
        if "temperature" in params:
            require_nonnegative_number(f"{name}.params.temperature", params["temperature"])
        if "timeout" in params:
            require_nonnegative_number(f"{name}.params.timeout", params["timeout"])
    elif job_type == "checkpoint_thread":
        require_keys(name, params, ["thread_id", "mode"])
        if params["mode"] not in {"soft", "hard"}:
            raise ValidationError(f"{name}.params.mode must be soft or hard")
        if "slot" in params:
            require_nonnegative_int(f"{name}.params.slot", params["slot"])
    elif job_type == "shutdown_thread":
        require_keys(name, params, ["thread_id"])
        if "slot" in params:
            require_nonnegative_int(f"{name}.params.slot", params["slot"])
        if "timeout" in params:
            require_nonnegative_number(f"{name}.params.timeout", params["timeout"])
        if "force" in params:
            require_bool(f"{name}.params.force", params["force"])
    elif job_type == "export_thread":
        require_keys(name, params, ["thread_id", "out"])
        for key in ["include_snapshots", "redact_transcript", "force"]:
            if key in params:
                require_bool(f"{name}.params.{key}", params[key])
    elif job_type == "validate_capsule":
        require_keys(name, params, ["thread_id"])
        if "require_snapshot" in params:
            require_bool(f"{name}.params.require_snapshot", params["require_snapshot"])
    elif job_type == "gateway_export_bundle":
        require_keys(name, params, ["gateway_url", "thread_id"])
        for key in ["include_snapshots", "redact_transcript", "force"]:
            if key in params:
                require_bool(f"{name}.params.{key}", params[key])
    elif job_type == "gateway_list_bundles":
        require_keys(name, params, ["gateway_url"])
    elif job_type == "gateway_download_bundle":
        require_keys(name, params, ["gateway_url", "bundle_id", "out"])
    elif job_type == "gateway_import_bundle":
        require_keys(name, params, ["gateway_url"])
        if "bundle" not in params and "bundle_id" not in params:
            raise ValidationError(f"{name}.params must include either bundle or bundle_id")
        if "force" in params:
            require_bool(f"{name}.params.force", params["force"])
    elif job_type == "gateway_delete_bundle":
        require_keys(name, params, ["gateway_url", "bundle_id"])


def validate_secret_ref(name: str, data: dict[str, Any]) -> None:
    require_keys(name, data, ["source", "ref"])
    if data["source"] not in {"none", "file", "env"}:
        raise ValidationError(f"{name}.source must be none, file, or env")
    if data["source"] == "none":
        if data["ref"] is not None:
            raise ValidationError(f"{name}.ref must be null when source is none")
    elif not isinstance(data["ref"], str) or not data["ref"]:
        raise ValidationError(f"{name}.ref must be a non-empty string when source is file or env")
    for key in data:
        if key in {"value", "token_value", "key_value", "secret"}:
            raise ValidationError(f"{name} must contain only secret references, not secret values")


def validate_gateway_launch_profile(path: Path) -> None:
    data = load_json(path)
    name = path.name
    require_keys(name, data, ["schema_version", "profile_id", "profile_type", "created_at", "gateway", "transport", "security"])
    if data["schema_version"] != "0.1":
        raise ValidationError(f"{name} schema_version must be 0.1")
    if data["profile_type"] != "session_capsule_gateway":
        raise ValidationError(f"{name}.profile_type must be session_capsule_gateway")

    if "command" in data:
        command = data["command"]
        if not isinstance(command, dict):
            raise ValidationError(f"{name}.command must be an object")
        require_keys(f"{name}.command", command, ["program", "args"])
        if not isinstance(command["program"], str) or not command["program"]:
            raise ValidationError(f"{name}.command.program must be a non-empty string")
        if not isinstance(command["args"], list) or not all(isinstance(item, str) for item in command["args"]):
            raise ValidationError(f"{name}.command.args must be a string array")

    gateway = data["gateway"]
    if not isinstance(gateway, dict):
        raise ValidationError(f"{name}.gateway must be an object")
    require_keys(
        f"{name}.gateway",
        gateway,
        ["state_dir", "endpoint_id", "host", "port", "checkpoint_mode", "slot", "timeout_seconds", "max_bundle_bytes"],
    )
    if gateway["host"] != "127.0.0.1":
        raise ValidationError(f"{name}.gateway.host should default to 127.0.0.1 for local-first launch profiles")
    if gateway["checkpoint_mode"] not in {"none", "soft", "hard"}:
        raise ValidationError(f"{name}.gateway.checkpoint_mode must be none, soft, or hard")
    if not isinstance(gateway["port"], int) or not 1 <= gateway["port"] <= 65535:
        raise ValidationError(f"{name}.gateway.port must be a TCP port integer")
    require_nonnegative_int(f"{name}.gateway.slot", gateway["slot"])
    require_nonnegative_number(f"{name}.gateway.timeout_seconds", gateway["timeout_seconds"])
    if not isinstance(gateway["max_bundle_bytes"], str) or not re.fullmatch(r"\d+(\.\d+)?\s*([KMGT]i?B|[KMGT]B|B)?", gateway["max_bundle_bytes"]):
        raise ValidationError(f"{name}.gateway.max_bundle_bytes must be a byte size string")

    transport = data["transport"]
    if not isinstance(transport, dict):
        raise ValidationError(f"{name}.transport must be an object")
    require_keys(f"{name}.transport", transport, ["openai_base_url", "status_url", "require_status_transport"])
    if not str(transport["openai_base_url"]).endswith("/v1"):
        raise ValidationError(f"{name}.transport.openai_base_url should point at the OpenAI-compatible /v1 path")
    if not str(transport["status_url"]).endswith("/api/capsules/status"):
        raise ValidationError(f"{name}.transport.status_url should point at /api/capsules/status")
    require_bool(f"{name}.transport.require_status_transport", transport["require_status_transport"])

    security = data["security"]
    if not isinstance(security, dict):
        raise ValidationError(f"{name}.security must be an object")
    require_keys(f"{name}.security", security, ["request_auth", "bundle_signing"])
    request_auth = security["request_auth"]
    if not isinstance(request_auth, dict):
        raise ValidationError(f"{name}.security.request_auth must be an object")
    validate_secret_ref(f"{name}.security.request_auth", request_auth)
    bundle_signing = security["bundle_signing"]
    if not isinstance(bundle_signing, dict):
        raise ValidationError(f"{name}.security.bundle_signing must be an object")
    validate_secret_ref(f"{name}.security.bundle_signing", bundle_signing)
    if "require_on_import" not in bundle_signing:
        raise ValidationError(f"{name}.security.bundle_signing is missing require_on_import")
    require_bool(f"{name}.security.bundle_signing.require_on_import", bundle_signing["require_on_import"])
    if bundle_signing["source"] == "none" and bundle_signing["require_on_import"]:
        raise ValidationError(f"{name}.security.bundle_signing cannot require signed imports without a key source")


def main() -> None:
    validate_schema_files()

    endpoint = load_json(EXAMPLES / "endpoint-capabilities.example.json")
    config = load_json(EXAMPLES / "capsule-config.example.json")
    capsule = load_json(EXAMPLES / "capsule-manifest.example.json")
    prefill = load_json(EXAMPLES / "prefill-manifest.example.json")
    thread = load_json(EXAMPLES / "thread-ledger.example.json")

    validate_endpoint(endpoint)
    validate_config(config)
    validate_capsule(capsule, endpoint)
    validate_capsule(prefill, endpoint)
    if "prefill_source" not in prefill:
        raise ValidationError("prefill example must include prefill_source")
    if prefill["kind"] not in {"user_prefill", "project_prefill"}:
        raise ValidationError("prefill example kind must be a prefill kind")
    validate_thread(thread, capsule, endpoint)

    for path in sorted((EXAMPLES / "model-plane").glob("*.example.json")):
        data = load_json(path)
        if data.get("profile_type") == "session_capsule_gateway":
            validate_gateway_launch_profile(path)
        else:
            validate_model_plane_job(path)

    print("schema examples ok")


if __name__ == "__main__":
    main()
