#!/usr/bin/env python3
"""Local OpenAI-compatible Session Capsules gateway.

The gateway sits in the request path:

client -> capsule gateway -> configured model endpoint

It manages thread ledgers, optional prefill roots, restore/delta forwarding when
a hard capsule exists, response transcript capture, and post-response
checkpointing.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import re
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import unquote, urlparse

import capsule_cli as cc


JSONDict = dict[str, Any]


THREAD_HEADER_CANDIDATES = [
    "X-Capsule-Thread",
    "X-OpenWebUI-Chat-Id",
    "X-Opencode-Thread",
    "X-Opencode-Session",
    "X-Session-Id",
    "X-Conversation-Id",
]

WORKSPACE_HEADER_CANDIDATES = [
    "X-Capsule-Workspace",
    "X-OpenWebUI-User-Id",
    "X-Opencode-Workspace",
    "X-Workspace-Id",
]

CORS_ALLOW_METHODS = "GET, POST, DELETE, OPTIONS"
CORS_ALLOW_HEADERS = ", ".join(
    [
        "Authorization",
        "Content-Type",
        "X-Capsule-Gateway-Key",
        "X-Capsule-Thread",
        "X-Capsule-Workspace",
        "X-Capsule-Prefill",
        "X-Capsule-Bundle-Id",
        "X-Capsule-Import-Force",
        "X-Capsule-Import-Thread",
        "X-OpenWebUI-Chat-Id",
        "X-OpenWebUI-User-Id",
        "X-Opencode-Thread",
        "X-Opencode-Session",
        "X-Opencode-Workspace",
    ]
)
CORS_EXPOSE_HEADERS = ", ".join(
    [
        "Content-Disposition",
        "X-Capsule-Bundle-Id",
        "X-Capsule-Bundle-SHA256",
        "X-Capsule-Thread",
        "X-Capsule-Mode",
        "X-Capsule-Checkpoint",
        "X-Capsule-Export",
        "X-Capsule-Import",
    ]
)


@dataclass
class GatewayConfig:
    state_dir: Path
    endpoint_id: str
    host: str
    port: int
    slot: int
    checkpoint_mode: str
    timeout: float
    default_prefill: str | None
    default_thread_prefix: str
    max_bundle_bytes: int
    signature_key_file: Path | None
    signature_key_env: str | None
    signature_key_id: str | None
    require_bundle_signature: bool
    auth_token: str | None
    lock: threading.Lock
    cors_allow_origin: str | None = None
    bundle_policy_preset: str = "report"
    bundle_policy_disallow_plaintext: bool = False
    bundle_policy_disallow_snapshots: bool = False
    bundle_policy_require_encryption: bool = False
    bundle_policy_require_digest_index: bool = False


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def request_json(url: str, payload: JSONDict, timeout: float) -> tuple[int, dict[str, str], bytes]:
    encoded = json_bytes(payload)
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            headers = {key: value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except error.HTTPError as exc:
        return exc.code, {key: value for key, value in exc.headers.items()}, exc.read()


def safe_header_id(value: str | None) -> str | None:
    if value is None:
        return None
    return cc.slugify(value)


def first_header(handler: BaseHTTPRequestHandler, candidates: list[str]) -> tuple[str | None, str | None]:
    for header in candidates:
        value = handler.headers.get(header)
        if value:
            return header, value
    return None, None


def generated_thread_id(config: GatewayConfig, body: JSONDict) -> str:
    model = str(body.get("model", "unknown"))
    messages = body.get("messages", [])
    seed = model
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            seed += ":" + str(first.get("role", "")) + ":" + str(first.get("content", ""))[:120]
    digest = cc.digest_text(seed).split(":", 1)[1][:12]
    return f"{config.default_thread_prefix}-{digest}"


def read_body(handler: BaseHTTPRequestHandler) -> JSONDict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    data = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def read_raw_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        raise ValueError("Request body is empty")
    if length > max_bytes:
        raise ValueError(f"Request body exceeds max bundle size: {length} > {max_bytes}")
    return handler.rfile.read(length)


def send_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    body = json_bytes(payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in getattr(handler, "_capsule_cors_headers", {}).items():
        handler.send_header(key, value)
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_bytes(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    status: int,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in getattr(handler, "_capsule_cors_headers", {}).items():
        handler.send_header(key, value)
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def read_gateway_auth_token(token_file: Path | None, token_env: str | None) -> str | None:
    if token_file and token_env:
        raise RuntimeError("Use only one gateway auth token source: --auth-token-file or --auth-token-env")
    token: str | None = None
    if token_file:
        token = token_file.read_text(encoding="utf-8").strip()
    elif token_env:
        token = os.environ.get(token_env)
        if token is None:
            raise RuntimeError(f"Gateway auth token environment variable is not set: {token_env}")
        token = token.strip()
    if token is not None and not token:
        raise RuntimeError("Gateway auth token is empty")
    return token


def request_auth_token(handler: BaseHTTPRequestHandler) -> str | None:
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    capsule_key = handler.headers.get("X-Capsule-Gateway-Key")
    if capsule_key:
        return capsule_key.strip()
    return None


def authorize_gateway_request(handler: BaseHTTPRequestHandler, config: GatewayConfig) -> bool:
    if config.auth_token is None:
        return True
    supplied = request_auth_token(handler)
    if supplied is not None and hmac.compare_digest(supplied, config.auth_token):
        return True
    send_json(
        handler,
        {"error": {"message": "unauthorized", "type": "gateway_auth"}},
        status=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
    return False


def ensure_endpoint(config: GatewayConfig) -> JSONDict:
    store = cc.Store(config.state_dir)
    return store.load_endpoint(config.endpoint_id)


def bundles_dir(config: GatewayConfig) -> Path:
    path = config.state_dir / "bundles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_bundle_id(value: str) -> str:
    raw_id = value.strip()
    if raw_id.lower().endswith(".scap"):
        raw_id = raw_id[:-5]
    bundle_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw_id).strip("-")
    if not bundle_id or bundle_id in {".", ".."}:
        raise ValueError("Invalid bundle id")
    return bundle_id


def bundle_path(config: GatewayConfig, bundle_id: str) -> Path:
    safe_id = safe_bundle_id(bundle_id)
    path = (bundles_dir(config) / f"{safe_id}.scap").resolve()
    root = bundles_dir(config).resolve()
    if root not in path.parents:
        raise ValueError("Invalid bundle path")
    return path


def new_bundle_id(thread_id: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return cc.slugify(f"{thread_id}-{stamp}-{uuid.uuid4().hex[:8]}")


def bundle_metadata(config: GatewayConfig, path: Path) -> JSONDict:
    bundle_id = path.stem
    metadata: JSONDict = {
        "bundle_id": bundle_id,
        "filename": path.name,
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "sha256": cc.digest_file(path) if path.exists() else None,
        "download_url": f"/api/capsules/bundles/{bundle_id}",
        "created_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        if path.exists()
        else None,
    }
    if path.exists():
        with contextlib.suppress(Exception):
            report = cc.inspect_bundle_report(path)
            integrity = report["integrity"]
            content = report["content"]
            policy = report["share_policy"]
            metadata.update(
                {
                    "thread_id": report.get("thread_id"),
                    "export_mode": report.get("export_mode"),
                    "includes_snapshots": content.get("snapshots_included"),
                    "redacted_transcript": report.get("redacted_transcript"),
                    "transcript_included": content.get("transcript_included"),
                    "prefill_sources_included": content.get("prefill_sources_included"),
                    "signature_present": integrity.get("signature_present"),
                    "signature_algorithm": integrity.get("signature_algorithm"),
                    "signature_key_id": integrity.get("signature_key_id"),
                    "encrypted": integrity.get("encrypted"),
                    "share_safety": policy.get("classification"),
                    "trusted_transport_required": policy.get("trusted_transport_required"),
                }
            )
    return metadata


def list_bundles(config: GatewayConfig) -> list[JSONDict]:
    return [bundle_metadata(config, path) for path in sorted(bundles_dir(config).glob("*.scap"))]


def cors_response_headers(handler: BaseHTTPRequestHandler, config: GatewayConfig) -> dict[str, str]:
    if not config.cors_allow_origin:
        return {}
    origin = handler.headers.get("Origin")
    if config.cors_allow_origin == "*":
        allowed_origin = "*"
    elif origin is None:
        allowed_origin = config.cors_allow_origin
    elif origin == config.cors_allow_origin:
        allowed_origin = origin
    else:
        return {}

    headers = {
        "Access-Control-Allow-Origin": allowed_origin,
        "Access-Control-Allow-Methods": CORS_ALLOW_METHODS,
        "Access-Control-Allow-Headers": CORS_ALLOW_HEADERS,
        "Access-Control-Expose-Headers": CORS_EXPOSE_HEADERS,
        "Access-Control-Max-Age": "600",
    }
    if allowed_origin != "*":
        headers["Vary"] = "Origin"
    return headers


def transport_contract(config: GatewayConfig) -> JSONDict:
    signing_enabled = bool(config.signature_key_file or config.signature_key_env)
    return {
        "api_version": "0.1",
        "bundle_format": "session-capsules.scap",
        "bundle_content_type": "application/vnd.session-capsule.scap",
        "bundle_store": "bundles/",
        "max_upload_bytes": config.max_bundle_bytes,
        "capabilities": {
            "export": True,
            "list": True,
            "download": True,
            "raw_upload_import": True,
            "stored_bundle_import": True,
            "delete": True,
            "thread_id_override": True,
            "digest_verification": True,
            "hmac_sha256_signing": signing_enabled,
            "require_signature_on_import": config.require_bundle_signature,
            "bundle_policy_gate": True,
        },
        "endpoints": {
            "status": {"method": "GET", "path": "/api/capsules/status"},
            "threads": {"method": "GET", "path": "/api/capsules/threads"},
            "checkpoint": {"method": "POST", "path": "/api/capsules/checkpoint"},
            "export": {"method": "POST", "path": "/api/capsules/export"},
            "list_bundles": {"method": "GET", "path": "/api/capsules/bundles"},
            "download_bundle": {"method": "GET", "path_template": "/api/capsules/bundles/{bundle_id}"},
            "import": {"method": "POST", "path": "/api/capsules/import"},
            "delete_bundle": {"method": "DELETE", "path_template": "/api/capsules/bundles/{bundle_id}"},
        },
        "export_defaults": {
            "include_snapshots": False,
            "redact_transcript": False,
        },
        "auth": {
            "required": config.auth_token is not None,
            "accepted_headers": ["Authorization: Bearer TOKEN", "X-Capsule-Gateway-Key"],
        },
        "cors": {
            "enabled": config.cors_allow_origin is not None,
            "allow_origin": config.cors_allow_origin,
            "preflight": config.cors_allow_origin is not None,
            "allowed_methods": CORS_ALLOW_METHODS.split(", "),
            "allowed_headers": CORS_ALLOW_HEADERS.split(", "),
            "exposed_headers": CORS_EXPOSE_HEADERS.split(", "),
        },
        "signing": {
            "exports_signed": signing_enabled,
            "signature_key_id": config.signature_key_id if signing_enabled else None,
            "required_on_import": config.require_bundle_signature,
        },
        "import_policy": bundle_import_policy(config),
    }


def bundle_import_policy(config: GatewayConfig) -> JSONDict:
    requirements = cc.bundle_policy_requirements(
        config.bundle_policy_preset,
        config.bundle_policy_disallow_plaintext,
        config.bundle_policy_disallow_snapshots,
        config.require_bundle_signature,
        config.bundle_policy_require_encryption,
        config.bundle_policy_require_digest_index,
    )
    return {
        "preset": config.bundle_policy_preset,
        "requirements": sorted(requirements),
        "disallow_plaintext": config.bundle_policy_disallow_plaintext,
        "disallow_snapshots": config.bundle_policy_disallow_snapshots,
        "require_signature": "require_signature" in requirements,
        "verify_signature": config.require_bundle_signature,
        "require_encryption": config.bundle_policy_require_encryption,
        "require_digest_index": config.bundle_policy_require_digest_index,
    }


def identity_contract(config: GatewayConfig) -> JSONDict:
    return {
        "api_version": "0.1",
        "preferred_headers": {
            "thread": "X-Capsule-Thread",
            "workspace": "X-Capsule-Workspace",
            "prefill": "X-Capsule-Prefill",
        },
        "accepted_thread_headers": THREAD_HEADER_CANDIDATES,
        "accepted_workspace_headers": WORKSPACE_HEADER_CANDIDATES,
        "accepted_prefill_header": "X-Capsule-Prefill",
        "client_mappings": {
            "open_webui": {
                "minimum_thread_header": "X-OpenWebUI-Chat-Id",
                "optional_workspace_header": "X-OpenWebUI-User-Id",
            },
            "opencode": {
                "minimum_thread_headers": ["X-Opencode-Thread", "X-Opencode-Session"],
                "optional_workspace_header": "X-Opencode-Workspace",
            },
            "generic_openai": {
                "minimum_thread_header": "X-Capsule-Thread",
                "optional_workspace_header": "X-Capsule-Workspace",
            },
        },
        "fallback": {
            "generated_thread_id": True,
            "source": "model and first request message",
            "continuity": "best_effort",
        },
        "default_thread_prefix": config.default_thread_prefix,
        "default_prefill": config.default_prefill,
    }


def export_bundle_api(config: GatewayConfig, payload: JSONDict) -> JSONDict:
    thread_id = cc.slugify(str(payload["thread_id"]))
    bundle_id = safe_bundle_id(str(payload.get("bundle_id") or new_bundle_id(thread_id)))
    out_path = bundle_path(config, bundle_id)
    args = argparse.Namespace(
        state_dir=config.state_dir,
        thread=thread_id,
        out=out_path,
        include_snapshots=bool(payload.get("include_snapshots", False)),
        redact_transcript=bool(payload.get("redact_transcript", False)),
        signature_key_file=config.signature_key_file,
        signature_key_env=config.signature_key_env,
        signature_key_id=config.signature_key_id,
        force=bool(payload.get("force", False)),
    )
    with config.lock:
        cc.export_bundle(args)
    metadata = bundle_metadata(config, out_path)
    metadata["thread_id"] = thread_id
    return metadata


def import_bundle_file(config: GatewayConfig, path: Path, force: bool = False, thread_id: str | None = None) -> JSONDict:
    cc.enforce_bundle_policy(
        path,
        config.bundle_policy_preset,
        config.bundle_policy_disallow_plaintext,
        config.bundle_policy_disallow_snapshots,
        config.require_bundle_signature,
        config.bundle_policy_require_encryption,
        config.bundle_policy_require_digest_index,
    )
    target_thread_id = cc.slugify(thread_id) if thread_id else None
    args = argparse.Namespace(
        state_dir=config.state_dir,
        bundle=path,
        thread_id=target_thread_id,
        signature_key_file=config.signature_key_file,
        signature_key_env=config.signature_key_env,
        require_signature=config.require_bundle_signature,
        force=force,
    )
    with config.lock:
        cc.import_bundle(args)
    with zipfile.ZipFile(path, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    imported_thread_id = target_thread_id or manifest["thread_id"]
    ledger = cc.Store(config.state_dir).load_ledger(imported_thread_id)
    return {
        "thread_id": imported_thread_id,
        "source_thread_id": manifest["thread_id"],
        "active_capsule_id": ledger.get("active_capsule_id"),
        "bundle_id": path.stem,
        "fallback": ledger.get("fallback", {}),
    }


def import_bundle_api(config: GatewayConfig, handler: BaseHTTPRequestHandler) -> JSONDict:
    content_type = handler.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = read_body(handler)
        bundle_id = str(payload["bundle_id"])
        path = bundle_path(config, bundle_id)
        if not path.exists():
            raise FileNotFoundError(f"Bundle not found: {bundle_id}")
        return import_bundle_file(
            config,
            path,
            bool(payload.get("force", False)),
            str(payload["thread_id"]) if payload.get("thread_id") else None,
        )

    body = read_raw_body(handler, config.max_bundle_bytes)
    requested_id = handler.headers.get("X-Capsule-Bundle-Id")
    bundle_id = safe_bundle_id(requested_id or f"upload-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    path = bundle_path(config, bundle_id)
    if path.exists():
        raise FileExistsError(f"Bundle already exists: {bundle_id}")
    path.write_bytes(body)
    try:
        return import_bundle_file(
            config,
            path,
            handler.headers.get("X-Capsule-Import-Force", "").lower() in {"1", "true", "yes"},
            handler.headers.get("X-Capsule-Import-Thread"),
        )
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise


def endpoint_url(endpoint: JSONDict, path: str) -> str:
    return endpoint["base_url"].rstrip("/") + path


def ensure_thread(
    config: GatewayConfig,
    thread_id: str,
    workspace: str | None,
    prefill: str | None,
) -> JSONDict:
    store = cc.Store(config.state_dir)
    ledger_path = store.ledger_path(thread_id)
    if ledger_path.exists():
        return store.load_ledger(thread_id)

    args = argparse.Namespace(
        state_dir=config.state_dir,
        endpoint=config.endpoint_id,
        thread_id=thread_id,
        name=thread_id,
        workspace=workspace,
        prefill=prefill,
        prefill_version=None,
        force=False,
    )
    result = cc.thread_start(args)
    if result != 0:
        raise RuntimeError(f"Could not start thread {thread_id}")
    return store.load_ledger(thread_id)


def append_request_messages(config: GatewayConfig, thread_id: str, messages: list[JSONDict]) -> None:
    store = cc.Store(config.state_dir)
    existing = cc.read_jsonl(store.transcript_path(thread_id))
    existing_pairs = [(row.get("role"), row.get("content")) for row in existing]
    incoming_pairs = [
        (message.get("role"), message.get("content"))
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    ]

    prefix_len = 0
    while prefix_len < len(existing_pairs) and prefix_len < len(incoming_pairs):
        if existing_pairs[prefix_len] != incoming_pairs[prefix_len]:
            break
        prefix_len += 1

    if prefix_len < len(existing_pairs):
        # The client did not send a clean transcript prefix. Preserve evidence
        # by appending the whole request as new request metadata instead of
        # trying to surgically rewrite history.
        content = json.dumps(messages, sort_keys=True)
        args = argparse.Namespace(
            state_dir=config.state_dir,
            thread=thread_id,
            role="user",
            content=content,
            file=None,
        )
        cc.thread_append(args)
        return

    for message in messages[prefix_len:]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        role = str(message.get("role", "user"))
        if not isinstance(content, str):
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        args = argparse.Namespace(
            state_dir=config.state_dir,
            thread=thread_id,
            role=role,
            content=content,
            file=None,
        )
        cc.thread_append(args)


def append_assistant_response(config: GatewayConfig, thread_id: str, response_payload: JSONDict) -> None:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    first = choices[0]
    if not isinstance(first, dict):
        return
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        content = message["content"]
    elif isinstance(first.get("text"), str):
        content = first["text"]
    else:
        return
    args = argparse.Namespace(
        state_dir=config.state_dir,
        thread=thread_id,
        role="assistant",
        content=content,
        file=None,
    )
    cc.thread_append(args)


def latest_hard_manifest(config: GatewayConfig, thread_id: str) -> tuple[JSONDict, JSONDict] | None:
    store = cc.Store(config.state_dir)
    ledger = store.load_ledger(thread_id)
    try:
        return cc.find_latest_restorable_manifest(store, ledger, None)
    except Exception:  # noqa: BLE001 - absence of hard capsule means replay fallback.
        return None


def replay_messages(config: GatewayConfig, thread_id: str) -> list[JSONDict]:
    store = cc.Store(config.state_dir)
    ledger = store.load_ledger(thread_id)
    rows = cc.read_jsonl(store.transcript_path(thread_id))
    messages: list[JSONDict] = []

    active = ledger.get("active_capsule_id")
    parent = cc.find_capsule_link(ledger, active)
    if parent and str(parent.get("kind", "")).endswith("_prefill"):
        manifest = cc.load_manifest_ref(store, parent["manifest_ref"])
        source = manifest.get("prefill_source", {})
        source_ref = source.get("source_ref")
        if source_ref:
            source_path = store.root / source_ref
            if source_path.exists():
                messages.append({"role": "system", "content": source_path.read_text(encoding="utf-8")})

    for row in rows:
        messages.append({"role": row["role"], "content": row["content"]})
    return messages


def prepare_backend_body(config: GatewayConfig, body: JSONDict, thread_id: str, endpoint: JSONDict) -> tuple[JSONDict, str]:
    hard = latest_hard_manifest(config, thread_id)
    outbound = dict(body)
    outbound["stream"] = False

    if hard is None:
        outbound["messages"] = replay_messages(config, thread_id)
        outbound["cache_prompt"] = False
        return outbound, "replay"

    _link, manifest = hard
    cc.assert_manifest_compatible(manifest, endpoint)
    storage = manifest["storage"]
    runtime_snapshot_ref = storage.get("runtime_snapshot_ref") or storage.get("snapshot_ref")
    if not runtime_snapshot_ref:
        outbound["messages"] = replay_messages(config, thread_id)
        outbound["cache_prompt"] = False
        return outbound, "replay-missing-snapshot"

    cc.slot_action(endpoint, config.slot, "restore", runtime_snapshot_ref, config.timeout)
    rows = cc.read_jsonl(cc.Store(config.state_dir).transcript_path(thread_id))
    diff = cc.diff_messages_after(rows, int(manifest["context"]["token_end"]))
    outbound["messages"] = diff
    outbound["cache_prompt"] = True
    outbound[endpoint.get("slot_api", {}).get("slot_field", "id_slot")] = config.slot
    return outbound, "restore"


def checkpoint_after_response(config: GatewayConfig, thread_id: str) -> str | None:
    if config.checkpoint_mode == "none":
        return None
    if config.checkpoint_mode == "hard":
        capsule_id = cc.create_hard_checkpoint(
            cc.Store(config.state_dir),
            thread_id,
            config.slot,
            None,
            config.timeout,
            None,
        )
        return capsule_id
    args = argparse.Namespace(
        state_dir=config.state_dir,
        thread=thread_id,
        capsule_id=None,
    )
    cc.checkpoint_soft(args)
    ledger = cc.Store(config.state_dir).load_ledger(thread_id)
    return ledger.get("active_capsule_id")


def handle_chat_completion(handler: BaseHTTPRequestHandler, config: GatewayConfig, body: JSONDict) -> None:
    if body.get("stream") is True:
        send_json(
            handler,
            {
                "error": {
                    "message": "Streaming is not implemented in the capsule gateway v0. Send stream=false.",
                    "type": "unsupported_streaming",
                }
            },
            status=400,
        )
        return

    messages = body.get("messages")
    if not isinstance(messages, list):
        send_json(handler, {"error": {"message": "messages must be a list"}}, status=400)
        return

    endpoint = ensure_endpoint(config)
    _thread_header, thread_header_value = first_header(handler, THREAD_HEADER_CANDIDATES)
    _workspace_header, workspace = first_header(handler, WORKSPACE_HEADER_CANDIDATES)
    thread_id = safe_header_id(thread_header_value) or generated_thread_id(config, body)
    prefill = safe_header_id(handler.headers.get("X-Capsule-Prefill")) or config.default_prefill

    with config.lock:
        ensure_thread(config, thread_id, workspace, prefill)
        append_request_messages(config, thread_id, messages)
        outbound, mode = prepare_backend_body(config, body, thread_id, endpoint)
        status, headers, response_body = request_json(
            endpoint_url(endpoint, "/v1/chat/completions"),
            outbound,
            config.timeout,
        )
        response_payload: JSONDict | None = None
        if status < 400:
            try:
                parsed = json.loads(response_body.decode("utf-8"))
                if isinstance(parsed, dict):
                    response_payload = parsed
            except json.JSONDecodeError:
                response_payload = None
        checkpoint_id = None
        if response_payload is not None:
            append_assistant_response(config, thread_id, response_payload)
            checkpoint_id = checkpoint_after_response(config, thread_id)

    response_headers = {
        "X-Capsule-Thread": thread_id,
        "X-Capsule-Mode": mode,
    }
    if checkpoint_id:
        response_headers["X-Capsule-Checkpoint"] = checkpoint_id
    content_type = headers.get("Content-Type", "application/json")
    send_bytes(handler, response_body, status, content_type, response_headers)


def thread_summaries(config: GatewayConfig) -> list[JSONDict]:
    store = cc.Store(config.state_dir)
    if not store.threads_dir.exists():
        return []
    summaries: list[JSONDict] = []
    for ledger_path in sorted(store.threads_dir.glob("*/thread-ledger.json")):
        ledger = cc.read_json(ledger_path)
        summaries.append(
            {
                "thread_id": ledger["thread_id"],
                "endpoint_id": ledger["endpoint_id"],
                "active_capsule_id": ledger.get("active_capsule_id"),
                "capsule_count": len(ledger.get("capsules", [])),
                "open_diff_count": len(ledger.get("open_diffs", [])),
                "updated_at": ledger.get("updated_at"),
            }
        )
    return summaries


def checkpoint_from_api(config: GatewayConfig, payload: JSONDict) -> JSONDict:
    thread_id = str(payload["thread_id"])
    mode = str(payload.get("mode", config.checkpoint_mode))
    slot = int(payload.get("slot", config.slot))
    with config.lock:
        if mode == "hard":
            capsule_id = cc.create_hard_checkpoint(
                cc.Store(config.state_dir),
                thread_id,
                slot,
                None,
                config.timeout,
                None,
            )
        elif mode == "soft":
            args = argparse.Namespace(state_dir=config.state_dir, thread=thread_id, capsule_id=None)
            cc.checkpoint_soft(args)
            capsule_id = cc.Store(config.state_dir).load_ledger(thread_id).get("active_capsule_id")
        else:
            raise ValueError("mode must be soft or hard")
    return {"thread_id": thread_id, "capsule_id": capsule_id, "mode": mode}


def make_handler(config: GatewayConfig) -> type[BaseHTTPRequestHandler]:
    class CapsuleGatewayHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def prepare_response_headers(self) -> None:
            self._capsule_cors_headers = cors_response_headers(self, config)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.prepare_response_headers()
            if config.cors_allow_origin and self._capsule_cors_headers:
                send_bytes(self, b"", 204, "text/plain")
                return
            send_json(self, {"error": {"message": "cors origin not allowed"}}, status=403)

        def do_GET(self) -> None:  # noqa: N802
            self.prepare_response_headers()
            if not authorize_gateway_request(self, config):
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/capsules/status":
                endpoint = ensure_endpoint(config)
                send_json(
                    self,
                    {
                        "status": "ok",
                        "state_dir": str(config.state_dir),
                        "endpoint_id": config.endpoint_id,
                        "endpoint_base_url": endpoint["base_url"],
                        "checkpoint_mode": config.checkpoint_mode,
                        "threads": len(thread_summaries(config)),
                        "bundles": len(list_bundles(config)),
                        "bundle_signing": bool(config.signature_key_file or config.signature_key_env),
                        "require_bundle_signature": config.require_bundle_signature,
                        "bundle_import_policy": bundle_import_policy(config),
                        "auth_required": config.auth_token is not None,
                        "transport": transport_contract(config),
                        "identity": identity_contract(config),
                    },
                )
                return
            if parsed.path == "/api/capsules/threads":
                send_json(self, {"threads": thread_summaries(config)})
                return
            if parsed.path == "/api/capsules/bundles":
                send_json(self, {"bundles": list_bundles(config)})
                return
            if parsed.path.startswith("/api/capsules/bundles/"):
                bundle_id = unquote(parsed.path.rsplit("/", 1)[-1])
                path = bundle_path(config, bundle_id)
                if not path.exists():
                    send_json(self, {"error": {"message": "bundle not found"}}, status=404)
                    return
                send_bytes(
                    self,
                    path.read_bytes(),
                    200,
                    "application/vnd.session-capsule.scap",
                    {
                        "Content-Disposition": f'attachment; filename="{path.name}"',
                        "X-Capsule-Bundle-Id": path.stem,
                        "X-Capsule-Bundle-SHA256": cc.digest_file(path),
                    },
                )
                return
            if parsed.path == "/v1/models":
                endpoint = ensure_endpoint(config)
                try:
                    with request.urlopen(endpoint_url(endpoint, "/v1/models"), timeout=config.timeout) as response:
                        send_bytes(
                            self,
                            response.read(),
                            response.status,
                            response.headers.get("Content-Type", "application/json"),
                        )
                except Exception:
                    send_json(self, {"data": []})
                return
            send_json(self, {"error": {"message": "not found"}}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            self.prepare_response_headers()
            try:
                if not authorize_gateway_request(self, config):
                    return
                parsed = urlparse(self.path)
                if parsed.path == "/api/capsules/import":
                    send_json(self, import_bundle_api(config, self), headers={"X-Capsule-Import": "ok"})
                    return
                body = read_body(self)
                if parsed.path == "/v1/chat/completions":
                    handle_chat_completion(self, config, body)
                    return
                if parsed.path == "/api/capsules/checkpoint":
                    send_json(self, checkpoint_from_api(config, body))
                    return
                if parsed.path == "/api/capsules/export":
                    send_json(self, export_bundle_api(config, body), headers={"X-Capsule-Export": "ok"})
                    return
                send_json(self, {"error": {"message": "not found"}}, status=404)
            except Exception as exc:  # noqa: BLE001 - gateway should return JSON errors.
                send_json(self, {"error": {"message": str(exc), "type": "gateway_error"}}, status=500)

        def do_DELETE(self) -> None:  # noqa: N802
            self.prepare_response_headers()
            try:
                if not authorize_gateway_request(self, config):
                    return
                parsed = urlparse(self.path)
                if parsed.path.startswith("/api/capsules/bundles/"):
                    bundle_id = unquote(parsed.path.rsplit("/", 1)[-1])
                    path = bundle_path(config, bundle_id)
                    if not path.exists():
                        send_json(self, {"deleted": False, "bundle_id": safe_bundle_id(bundle_id)}, status=404)
                        return
                    path.unlink()
                    send_json(self, {"deleted": True, "bundle_id": path.stem})
                    return
                send_json(self, {"error": {"message": "not found"}}, status=404)
            except Exception as exc:  # noqa: BLE001 - gateway should return JSON errors.
                send_json(self, {"error": {"message": str(exc), "type": "gateway_error"}}, status=500)

    return CapsuleGatewayHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Session Capsules OpenAI-compatible gateway.")
    parser.add_argument("--state-dir", type=Path, default=Path(".capsules"))
    parser.add_argument("--endpoint", required=True, help="Endpoint id from the capsule state directory.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--slot", type=int, default=0)
    parser.add_argument("--checkpoint-mode", choices=["none", "soft", "hard"], default="soft")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--default-prefill")
    parser.add_argument("--default-thread-prefix", default="gateway")
    parser.add_argument("--max-bundle-bytes", default="5GB", help="Maximum raw .scap upload accepted by /api/capsules/import.")
    parser.add_argument("--signature-key-file", type=Path, help="Optional local key file used to sign gateway exports and verify signed imports.")
    parser.add_argument("--signature-key-env", help="Optional environment variable containing the gateway bundle signing key.")
    parser.add_argument("--signature-key-id", help="Non-secret key label written into gateway-signed bundles.")
    parser.add_argument("--require-bundle-signature", action="store_true", help="Require uploaded or stored bundles to verify with the configured signature key before import.")
    parser.add_argument("--bundle-policy-preset", choices=sorted(cc.BUNDLE_POLICY_PRESETS), default="report", help="Import policy preset enforced before extracting uploaded or stored bundles.")
    parser.add_argument("--bundle-policy-disallow-plaintext", action="store_true", help="Reject imports that include transcript or prefill source text.")
    parser.add_argument("--bundle-policy-disallow-snapshots", action="store_true", help="Reject imports that include hard snapshot blobs.")
    parser.add_argument("--bundle-policy-require-encryption", action="store_true", help="Reject imports that do not report an encryption envelope.")
    parser.add_argument("--bundle-policy-require-digest-index", action="store_true", help="Reject imports that do not include a file_digests index.")
    parser.add_argument("--auth-token-file", type=Path, help="Optional local token file required for every gateway request.")
    parser.add_argument("--auth-token-env", help="Optional environment variable containing the gateway auth token.")
    parser.add_argument("--cors-allow-origin", help="Optional browser origin allowed to call gateway APIs, or * for local development.")
    return parser


def create_server(config: GatewayConfig) -> ThreadingHTTPServer:
    handler = make_handler(config)
    return ThreadingHTTPServer((config.host, config.port), handler)


def main() -> int:
    args = build_parser().parse_args()
    config = GatewayConfig(
        state_dir=args.state_dir.resolve(),
        endpoint_id=args.endpoint,
        host=args.host,
        port=args.port,
        slot=args.slot,
        checkpoint_mode=args.checkpoint_mode,
        timeout=args.timeout,
        default_prefill=args.default_prefill,
        default_thread_prefix=args.default_thread_prefix,
        max_bundle_bytes=cc.parse_bytes(args.max_bundle_bytes),
        signature_key_file=args.signature_key_file.resolve() if args.signature_key_file else None,
        signature_key_env=args.signature_key_env,
        signature_key_id=args.signature_key_id,
        require_bundle_signature=args.require_bundle_signature,
        auth_token=read_gateway_auth_token(
            args.auth_token_file.resolve() if args.auth_token_file else None,
            args.auth_token_env,
        ),
        lock=threading.Lock(),
        cors_allow_origin=args.cors_allow_origin,
        bundle_policy_preset=args.bundle_policy_preset,
        bundle_policy_disallow_plaintext=args.bundle_policy_disallow_plaintext,
        bundle_policy_disallow_snapshots=args.bundle_policy_disallow_snapshots,
        bundle_policy_require_encryption=args.bundle_policy_require_encryption,
        bundle_policy_require_digest_index=args.bundle_policy_require_digest_index,
    )
    # Fail fast if endpoint is not configured.
    ensure_endpoint(config)
    server = create_server(config)
    print(f"capsule gateway listening on http://{config.host}:{config.port}")
    print(f"endpoint: {config.endpoint_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping capsule gateway")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
