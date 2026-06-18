#!/usr/bin/env python3
"""Minimal Session Capsules CLI ledger.

This is the Stage 2/3 harness. It manages endpoints, thread ledgers,
transcripts, soft checkpoints, and local llama.cpp hard checkpoints.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote


JSONDict = dict[str, Any]


CAPABILITY_KEYS = [
    "soft_capsules",
    "server_side_handles",
    "slot_save_restore",
    "user_carried_blobs",
    "sealed_blobs",
    "transcript_replay_fallback",
]

DEFAULT_CONFIG: JSONDict = {
    "schema_version": "0.1",
    "storage": {
        "max_bytes": "50GB",
        "min_free_bytes": "20GB",
        "prune_policy": "oldest_unpinned_first",
        "keep_latest_per_thread": 1,
        "protect_active_prefills": True,
    },
}

CONFIG_SETTERS = {
    "storage.max_bytes",
    "storage.min_free_bytes",
    "storage.prune_policy",
    "storage.keep_latest_per_thread",
    "storage.protect_active_prefills",
}

BYTE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}

SECRET_JOB_PARAM_KEYS = {
    "auth_token",
    "auth_token_env",
    "auth_token_file",
    "gateway_auth_token",
    "gateway_auth_token_env",
    "gateway_auth_token_file",
    "signature_key",
    "signature_key_env",
    "signature_key_file",
}


@dataclass
class BundleEntry:
    name: str
    data: bytes | None = None
    source: Path | None = None

    @property
    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        if self.source is not None and self.source.exists():
            return self.source.stat().st_size
        return 0


@dataclass
class ExportBundlePlan:
    entries: list[BundleEntry]
    included_files: list[str]
    omitted_snapshots: list[str]
    payload_bytes: int

HELP_TOPICS: dict[str, str] = {
    "overview": """Session Capsules keep the transcript canonical and treat hard runtime snapshots as acceleration.

Core objects:
  endpoint   where the model server lives
  thread     canonical transcript plus capsule ledger
  capsule    checkpoint manifest, optionally pointing at a hard snapshot
  prefill    reusable root capsule for stable user/project context
  gateway    local OpenAI-compatible request-path layer
  transport  gateway .scap upload/download API
  security   bundle integrity now, signing/encryption later

Start here:
  py -3 .\\scripts\\capsule_cli.py config init
  py -3 .\\scripts\\capsule_cli.py endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080
  py -3 .\\scripts\\capsule_cli.py thread start --endpoint local-llamacpp --name research-loop
  py -3 .\\scripts\\capsule_cli.py inspect

More:
  capsule help config
  capsule help gateway
  capsule help transport
  capsule help security
  capsule help storage
  capsule help model-plane""",
    "config": """Persistent config lives under:
  .capsules/config/settings.json

Use persistent config for policy that should survive restarts:
  storage.max_bytes
  storage.min_free_bytes
  storage.prune_policy
  storage.keep_latest_per_thread
  storage.protect_active_prefills

Commands:
  py -3 .\\scripts\\capsule_cli.py config init
  py -3 .\\scripts\\capsule_cli.py config show
  py -3 .\\scripts\\capsule_cli.py config show storage.max_bytes
  py -3 .\\scripts\\capsule_cli.py config set storage.max_bytes 50GB

Launch-specific values stay as flags or Model Plane launch profile fields:
  --host, --port, --endpoint, --slot, --checkpoint-mode, --timeout""",
    "endpoint": """Endpoint records describe a model target. They do not contain model weights.

Add an endpoint:
  py -3 .\\scripts\\capsule_cli.py endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080

Useful metadata:
  --runtime-build
  --model-ref
  --model-hash
  --tokenizer-hash
  --context-limit
  --slot-field

Check hard capsule support:
  py -3 .\\scripts\\capsule_cli.py endpoint doctor local-llamacpp --strict""",
    "thread": """A thread is the canonical transcript and capsule chain.

Start:
  py -3 .\\scripts\\capsule_cli.py thread start --endpoint local-llamacpp --name research-loop

Append:
  py -3 .\\scripts\\capsule_cli.py thread append --thread research-loop --role user --content "Initial request"

Checkpoint:
  py -3 .\\scripts\\capsule_cli.py checkpoint --thread research-loop --soft
  py -3 .\\scripts\\capsule_cli.py checkpoint --thread research-loop --hard --slot 0

Inspect:
  py -3 .\\scripts\\capsule_cli.py inspect --thread research-loop""",
    "prefill": """A prefill is reusable stable context used as a root or early parent capsule.

Create a soft prefill:
  py -3 .\\scripts\\capsule_cli.py prefill create --endpoint local-llamacpp --name user_default --input .\\user_prefill.md --soft

Create a hard local prefill:
  py -3 .\\scripts\\capsule_cli.py prefill create --endpoint local-llamacpp --name user_default --input .\\user_prefill.md --hard --slot 0

Use it when starting a thread:
  py -3 .\\scripts\\capsule_cli.py thread start --endpoint local-llamacpp --prefill user_default --name project-thread

Changed prefill text should create a new version, not mutate history:
  py -3 .\\scripts\\capsule_cli.py prefill diff --name user_default --input .\\user_prefill.md""",
    "gateway": """The gateway is a local OpenAI-compatible request-path layer.

It owns:
  thread identity
  restore/checkpoint policy
  transcript diffs
  fallback replay

The model server still owns:
  model weights
  tokenizer/runtime internals
  live KV cache
  slots
  generation

Run soft mode:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode soft

Run hard local mode:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode hard --slot 0

Client base URL:
  http://127.0.0.1:8765/v1

Optional headers:
  X-Capsule-Thread
  X-Capsule-Workspace
  X-Capsule-Prefill

For gateway upload/download endpoints:
  capsule help transport""",
    "transport": """Gateway transport lets a local UI or Model Plane move .scap bundles without reimplementing export/import.

Endpoints:
  GET    /api/capsules/status
  POST   /api/capsules/export
  GET    /api/capsules/bundles
  GET    /api/capsules/bundles/{bundle_id}
  POST   /api/capsules/import
  DELETE /api/capsules/bundles/{bundle_id}

Model Plane should read /api/capsules/status first. The response includes a versioned transport object with endpoint paths, max_upload_bytes, content type, auth policy, signing policy, and advertised upload/download capabilities.

Bundles are stored under:
  .capsules/bundles/

Export is ledger-only by default. Hard snapshots require include_snapshots=true.

Raw upload content type:
  application/vnd.session-capsule.scap

Upload size limit:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --max-bundle-bytes 5GB""",
    "storage": """Hard snapshots can be large. They are managed cache artifacts unless pinned.

Defaults:
  storage.max_bytes = 50GB
  storage.min_free_bytes = 20GB
  storage.prune_policy = oldest_unpinned_first
  storage.keep_latest_per_thread = 1

Pinned capsules are always protected.
Transcripts, ledgers, and manifests are never deleted by GC.

Inspect:
  py -3 .\\scripts\\capsule_cli.py stats

Pin active capsule:
  py -3 .\\scripts\\capsule_cli.py pin --thread research-loop

Preview cleanup:
  py -3 .\\scripts\\capsule_cli.py gc --dry-run

Apply cleanup:
  py -3 .\\scripts\\capsule_cli.py gc --apply""",
    "bundles": """A .scap bundle exports a thread without transporting model weights.

Ledger-only export:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop.scap

Preview export size without writing:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop.scap --dry-run

Include local hard snapshots only when intentionally moving same-runtime blobs:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop.scap --include-snapshots

Import:
  py -3 .\\scripts\\capsule_cli.py import .\\research-loop.scap

Import warns when an incoming endpoint id already exists locally with different runtime metadata.

Verify bundle integrity:
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap

Sign with an explicit local key file:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop.scap --signature-key-file .\\capsule-signing.key --signature-key-id local

If snapshots are omitted, transcript replay remains the fallback.

For gateway upload/download transport:
  capsule help transport""",
    "security": """Security status:
  implemented: per-entry sha256 file_digests in exported .scap bundles
  implemented: optional HMAC-SHA256 bundle signatures
  implemented: capsule verify rejects duplicate or digest-mismatched bundle entries
  implemented: import verifies bundles that include file_digests
  implemented: import warns on local endpoint metadata conflicts
  not implemented yet: encryption or sealed user-carried blobs

Commands:
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap --signature-key-file .\\capsule-signing.key --require-signature

Key handling:
  --signature-key-file reads a local key file for this command only
  --signature-key-env reads a key from an environment variable
  keys are not written into .capsules state

HMAC signing proves possession of the shared key. Encryption and sealed blobs are future envelope layers.""",
    "model-plane": """Model Plane should supervise Session Capsules, not become the gateway.

Model Plane owns:
  launch profile
  endpoint registry
  process lifecycle
  health checks
  job routing policy

Capsule gateway owns:
  OpenAI-compatible request path
  thread ledger lookup
  restore/checkpoint
  transcript diffs
  fallback replay

Run a job packet:
  py -3 .\\scripts\\capsule_cli.py job run .\\examples\\model-plane\\checkpoint-thread.example.json --dry-run

Shutdown before unload:
  py -3 .\\scripts\\capsule_cli.py job run .\\examples\\model-plane\\shutdown-thread.example.json --dry-run

Signed export job packets:
  py -3 .\\scripts\\capsule_cli.py job run .\\examples\\model-plane\\export-thread.example.json --signature-key-file .\\capsule-signing.key --signature-key-id local

Protected gateway transport jobs:
  py -3 .\\scripts\\capsule_cli.py job run .\\examples\\model-plane\\gateway-download-bundle.example.json --gateway-auth-token-file .\\capsule-gateway-token

Gateway health endpoint for launch profiles:
  /api/capsules/status

Supported job packet types:
  resume_thread
  checkpoint_thread
  shutdown_thread
  export_thread
  validate_capsule
  gateway_export_bundle
  gateway_list_bundles
  gateway_download_bundle
  gateway_import_bundle
  gateway_delete_bundle""",
    "troubleshooting": """Common checks:

No endpoint:
  py -3 .\\scripts\\capsule_cli.py inspect

Hard restore not working:
  py -3 .\\scripts\\capsule_cli.py endpoint doctor local-llamacpp --strict
  resume --append-diff marks failed hard capsules restore_failed, replays the canonical transcript, and saves a replacement checkpoint.

Storage growing:
  py -3 .\\scripts\\capsule_cli.py stats
  py -3 .\\scripts\\capsule_cli.py gc --dry-run

Gateway client fails:
  Use stream=false for gateway v0.
  Check http://127.0.0.1:8765/api/capsules/status.
  Verify the client points at http://127.0.0.1:8765/v1.

Docker client cannot reach host gateway:
  Use http://host.docker.internal:8765/v1 from inside Docker.""",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip()).strip("-")
    return slug or f"thread-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def estimate_tokens(value: str) -> int:
    if not value:
        return 0
    # Soft checkpoints do not have tokenizer access yet. This is a stable
    # estimate used only for ledger ranges until a runtime adapter supplies
    # true token positions.
    return max(1, len(re.findall(r"\S+", value)))


def read_json(path: Path) -> JSONDict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[JSONDict]:
    if not path.exists():
        return []
    rows: list[JSONDict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(item)
    return rows


@dataclass
class Store:
    root: Path

    @property
    def endpoints_dir(self) -> Path:
        return self.root / "endpoints"

    @property
    def threads_dir(self) -> Path:
        return self.root / "threads"

    @property
    def prefills_dir(self) -> Path:
        return self.root / "prefills"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "settings.json"

    def endpoint_path(self, endpoint_id: str) -> Path:
        return self.endpoints_dir / f"{endpoint_id}.json"

    def thread_dir(self, thread_id: str) -> Path:
        return self.threads_dir / thread_id

    def ledger_path(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "thread-ledger.json"

    def transcript_path(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "transcript.jsonl"

    def manifests_dir(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "manifests"

    def snapshots_dir(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "snapshots"

    def prefill_dir(self, name: str) -> Path:
        return self.prefills_dir / slugify(name)

    def prefill_index_path(self, name: str) -> Path:
        return self.prefill_dir(name) / "index.json"

    def prefill_version_dir(self, name: str, version: str) -> Path:
        return self.prefill_dir(name) / version

    def relative_ref(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def load_endpoint(self, endpoint_id: str) -> JSONDict:
        path = self.endpoint_path(endpoint_id)
        if not path.exists():
            raise FileNotFoundError(f"Endpoint not found: {endpoint_id}")
        return read_json(path)

    def load_ledger(self, thread_id: str) -> JSONDict:
        path = self.ledger_path(thread_id)
        if not path.exists():
            raise FileNotFoundError(f"Thread not found: {thread_id}")
        return read_json(path)


def deep_copy_json(payload: JSONDict) -> JSONDict:
    return json.loads(json.dumps(payload))


def merge_config(defaults: JSONDict, overrides: JSONDict) -> JSONDict:
    merged = deep_copy_json(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(store: Store) -> JSONDict:
    if not store.config_path.exists():
        return deep_copy_json(DEFAULT_CONFIG)
    data = read_json(store.config_path)
    return merge_config(DEFAULT_CONFIG, data)


def write_config(store: Store, config: JSONDict) -> None:
    write_json(store.config_path, config)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Not a boolean value: {value}")


def parse_config_value(key: str, value: str) -> Any:
    if key == "storage.protect_active_prefills":
        return parse_bool(value)
    if key == "storage.keep_latest_per_thread":
        parsed = int(value)
        if parsed < 0:
            raise ValueError("storage.keep_latest_per_thread must be >= 0")
        return parsed
    if key == "storage.prune_policy":
        if value != "oldest_unpinned_first":
            raise ValueError("Only oldest_unpinned_first is supported")
        return value
    if key in {"storage.max_bytes", "storage.min_free_bytes"}:
        parse_bytes(value)
        return value
    return value


def set_nested(payload: JSONDict, key: str, value: Any) -> None:
    parts = key.split(".")
    target = payload
    for part in parts[:-1]:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"Cannot set nested config under non-object key: {part}")
        target = child
    target[parts[-1]] = value


def get_nested(payload: JSONDict, key: str) -> Any:
    target: Any = payload
    for part in key.split("."):
        if not isinstance(target, dict) or part not in target:
            raise KeyError(key)
        target = target[part]
    return target


def parse_bytes(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("byte value must be nonnegative")
        return int(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)?\s*", value)
    if not match:
        raise ValueError(f"Invalid byte size: {value}")
    amount = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    if unit not in BYTE_UNITS:
        raise ValueError(f"Unsupported byte unit: {unit}")
    return int(amount * BYTE_UNITS[unit])


def format_bytes(value: int) -> str:
    for label, scale in [("TB", 1000**4), ("GB", 1000**3), ("MB", 1000**2), ("KB", 1000)]:
        if value >= scale:
            return f"{value / scale:.2f}{label}"
    return f"{value}B"


def make_endpoint(args: argparse.Namespace) -> JSONDict:
    endpoint_type = args.type
    runtime_name = args.runtime_name or ("llama.cpp" if endpoint_type == "llamacpp" else endpoint_type)
    slot_save_restore = bool(args.slot_save_restore or endpoint_type == "llamacpp")
    return {
        "schema_version": "0.1",
        "endpoint_id": args.endpoint_id,
        "type": endpoint_type,
        "base_url": args.base_url,
        "runtime": {
            "name": runtime_name,
            "build": args.runtime_build,
            "model_ref": args.model_ref,
            "model_hash": args.model_hash,
            "tokenizer_hash": args.tokenizer_hash,
            "context_limit": args.context_limit,
        },
        "capabilities": {
            "soft_capsules": True,
            "server_side_handles": False,
            "slot_save_restore": slot_save_restore,
            "user_carried_blobs": False,
            "sealed_blobs": False,
            "transcript_replay_fallback": True,
        },
        "slot_api": {
            "slots_path": "/slots",
            "save_action": "save",
            "restore_action": "restore",
            "slot_field": args.slot_field,
        },
        "checked_at": now_iso(),
        "notes": [
            "Created by capsule_cli.py. Hard capabilities should be confirmed with endpoint doctor."
        ],
    }


def endpoint_add(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    path = store.endpoint_path(args.endpoint_id)
    if path.exists() and not args.force:
        print(f"Endpoint already exists: {path}", file=sys.stderr)
        return 2
    endpoint = make_endpoint(args)
    write_json(path, endpoint)
    print(f"wrote endpoint: {path}")
    return 0


def get_json(url: str, timeout: float) -> tuple[Any, float]:
    req = request.Request(url, method="GET")
    started = datetime.now()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc.reason}") from exc
    elapsed = (datetime.now() - started).total_seconds() * 1000
    return json.loads(body or "null"), round(elapsed, 3)


def post_json(url: str, payload: JSONDict, timeout: float) -> tuple[JSONDict, float]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = datetime.now()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc.reason}") from exc
    elapsed = (datetime.now() - started).total_seconds() * 1000
    parsed = json.loads(body or "{}")
    if not isinstance(parsed, dict):
        return {"response": parsed}, round(elapsed, 3)
    return parsed, round(elapsed, 3)


def slot_action(
    endpoint: JSONDict,
    slot_id: int,
    action: str,
    filename: str | None,
    timeout: float,
) -> tuple[JSONDict, float]:
    save_action = endpoint.get("slot_api", {}).get("save_action", "save")
    restore_action = endpoint.get("slot_api", {}).get("restore_action", "restore")
    if action == "save":
        runtime_action = save_action
    elif action == "restore":
        runtime_action = restore_action
    else:
        runtime_action = action
    payload: JSONDict = {}
    if filename is not None:
        payload["filename"] = filename
    url = f"{endpoint['base_url'].rstrip('/')}/slots/{slot_id}?action={runtime_action}"
    return post_json(url, payload, timeout)


def chat_completion(
    endpoint: JSONDict,
    slot_id: int,
    messages: list[JSONDict],
    max_tokens: int,
    temperature: float,
    timeout: float,
    chat_path: str,
    cache_prompt: bool = True,
) -> tuple[JSONDict, float]:
    slot_field = endpoint.get("slot_api", {}).get("slot_field", "id_slot")
    payload: JSONDict = {
        "messages": messages,
        "stream": False,
        "cache_prompt": cache_prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    payload[slot_field] = slot_id
    return post_json(f"{endpoint['base_url'].rstrip('/')}{chat_path}", payload, timeout)


def endpoint_doctor(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    endpoint = store.load_endpoint(args.endpoint_id)
    slot_api = endpoint.get("slot_api", {})
    slots_path = slot_api.get("slots_path", "/slots")
    url = endpoint["base_url"].rstrip("/") + slots_path
    endpoint["checked_at"] = now_iso()

    try:
        slots, elapsed_ms = get_json(url, args.timeout)
    except Exception as exc:  # noqa: BLE001 - doctor reports degraded state.
        endpoint["capabilities"]["slot_save_restore"] = False
        endpoint["notes"] = [
            *endpoint.get("notes", []),
            f"doctor could not reach {url}: {exc}",
        ]
        write_json(store.endpoint_path(args.endpoint_id), endpoint)
        print(f"endpoint reachable: no ({exc})")
        print("soft capsules remain available; hard slot restore is unverified")
        return 1 if args.strict else 0

    endpoint["capabilities"]["slot_save_restore"] = True
    endpoint["doctor"] = {
        "slots_url": url,
        "client_duration_ms": elapsed_ms,
        "slot_count": len(slots) if isinstance(slots, list) else None,
    }
    write_json(store.endpoint_path(args.endpoint_id), endpoint)
    print(f"endpoint reachable: yes ({elapsed_ms} ms)")
    if isinstance(slots, list):
        print(f"slots: {len(slots)}")
    else:
        print("slots response was not a list")
    return 0


def load_prefill_source(args: argparse.Namespace) -> str:
    if getattr(args, "input", None):
        return Path(args.input).read_text(encoding="utf-8")
    if getattr(args, "content", None) is not None:
        return args.content
    raise ValueError("Provide --input or --content")


def prefill_index_default(name: str) -> JSONDict:
    return {
        "schema_version": "0.1",
        "prefill_name": slugify(name),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "active_version": None,
        "versions": [],
    }


def load_prefill_index(store: Store, name: str) -> JSONDict:
    path = store.prefill_index_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Prefill not found: {name}")
    return read_json(path)


def next_prefill_version(index: JSONDict) -> str:
    count = len(index.get("versions", [])) + 1
    return f"v{count:03d}"


def prefill_manifest_ref(name: str, version: str) -> str:
    return (Path("prefills") / slugify(name) / version / "manifest.json").as_posix()


def prefill_source_ref(name: str, version: str) -> str:
    return (Path("prefills") / slugify(name) / version / "source.md").as_posix()


def latest_prefill_link(index: JSONDict, version: str | None = None) -> JSONDict:
    versions = index.get("versions", [])
    if version is None:
        active = index.get("active_version")
        if active is not None:
            version = active
    for item in reversed(versions):
        if version is None or item["version"] == version:
            return item
    raise FileNotFoundError(f"Prefill version not found: {version or '<latest>'}")


def resolve_prefill(
    store: Store,
    name: str,
    version: str | None,
    endpoint_id: str | None = None,
) -> tuple[JSONDict, JSONDict]:
    index = load_prefill_index(store, name)
    link = latest_prefill_link(index, version)
    manifest = read_json(store.root / link["manifest_ref"])
    if endpoint_id is not None and manifest["endpoint_id"] != endpoint_id:
        raise RuntimeError(
            f"Prefill endpoint mismatch: {manifest['endpoint_id']} != {endpoint_id}"
        )
    return link, manifest


def write_prefill_index(store: Store, name: str, index: JSONDict) -> None:
    index["updated_at"] = now_iso()
    write_json(store.prefill_index_path(name), index)


def prefill_create(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    endpoint = store.load_endpoint(args.endpoint)
    source = load_prefill_source(args)
    name = slugify(args.name)
    index_path = store.prefill_index_path(name)
    index = read_json(index_path) if index_path.exists() else prefill_index_default(name)
    version = args.version or next_prefill_version(index)
    version_dir = store.prefill_version_dir(name, version)
    manifest_path = version_dir / "manifest.json"
    if manifest_path.exists() and not args.force:
        raise FileExistsError(f"Prefill version already exists: {name} {version}")

    source_path = version_dir / "source.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source, encoding="utf-8")

    token_end = estimate_tokens(source)
    capsule_id = f"prefill_{name}_{version}"
    source_digest = digest_text(source)
    storage: JSONDict = {
        "mode": "soft",
        "snapshot_ref": None,
        "runtime_snapshot_ref": None,
        "snapshot_bytes": None,
        "snapshot_digest": None,
    }
    notes = [
        "Prefill capsule source is stored for audit and transcript replay fallback."
    ]
    slot_format = "soft-prefill-source"

    if args.hard:
        if not endpoint.get("capabilities", {}).get("slot_save_restore", False):
            raise RuntimeError("Endpoint does not advertise slot_save_restore. Run endpoint doctor or use --soft.")
        prefill_response, prefill_ms = chat_completion(
            endpoint,
            args.slot,
            [{"role": args.role, "content": source}],
            0,
            args.temperature,
            args.timeout,
            args.chat_path,
        )
        snapshot_path = version_dir / "snapshot.bin"
        runtime_snapshot_ref = args.runtime_filename or str(snapshot_path.resolve())
        save_response, save_ms = slot_action(endpoint, args.slot, "save", runtime_snapshot_ref, args.timeout)
        snapshot_bytes, snapshot_digest = snapshot_metadata(snapshot_path, save_response)
        storage = {
            "mode": "local_file",
            "snapshot_ref": store.relative_ref(snapshot_path),
            "runtime_snapshot_ref": runtime_snapshot_ref,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_digest": snapshot_digest,
        }
        slot_format = "llama.cpp-prefill-slot-save-restore"
        notes.extend(
            [
                f"Hard prefill loaded into slot {args.slot} before save.",
                f"prefill completion client duration: {prefill_ms} ms.",
                f"llama.cpp save client duration: {save_ms} ms.",
            ]
        )
        if prefill_response.get("choices"):
            notes.append("Runtime returned a chat completion response while compiling the prefill.")

    manifest: JSONDict = {
        "schema_version": "0.1",
        "capsule_id": capsule_id,
        "thread_id": f"prefill:{name}",
        "kind": args.kind,
        "parent_capsule_id": None,
        "endpoint_id": endpoint["endpoint_id"],
        "created_at": now_iso(),
        "expires_at": None,
        "compatibility": compatibility_from_endpoint(endpoint, token_end, slot_format),
        "context": {
            "token_start": 0,
            "token_end": token_end,
            "token_count": token_end,
            "token_digest": source_digest,
            "segments": [
                {
                    "segment_id": f"{name}_{version}_source",
                    "source": "prefill",
                    "role": args.role,
                    "token_start": 0,
                    "token_end": token_end,
                    "digest": source_digest,
                }
            ],
        },
        "prefill_source": {
            "name": name,
            "version": version,
            "source_ref": store.relative_ref(source_path),
            "source_digest": source_digest,
        },
        "storage": storage,
        "security": {
            "sealed": False,
            "signature": None,
            "encryption": None,
        },
        "notes": notes,
    }
    write_json(manifest_path, manifest)

    index["active_version"] = version
    existing_versions = [item for item in index.get("versions", []) if item["version"] != version]
    for item in existing_versions:
        if item.get("status") == "active":
            item["status"] = "superseded"
    existing_versions.append(
        {
            "version": version,
            "capsule_id": capsule_id,
            "manifest_ref": prefill_manifest_ref(name, version),
            "source_ref": prefill_source_ref(name, version),
            "source_digest": source_digest,
            "endpoint_id": endpoint["endpoint_id"],
            "kind": args.kind,
            "storage_mode": storage["mode"],
            "created_at": manifest["created_at"],
            "status": "active",
        }
    )
    index["versions"] = existing_versions
    write_prefill_index(store, name, index)
    print(f"created prefill: {name} {version} ({storage['mode']})")
    return 0


def prefill_list(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    if not store.prefills_dir.exists():
        print("prefills: 0")
        return 0
    count = 0
    for index_path in sorted(store.prefills_dir.glob("*/index.json")):
        index = read_json(index_path)
        count += 1
        active = index.get("active_version")
        print(f"{index['prefill_name']} active={active} versions={len(index.get('versions', []))}")
        if args.verbose:
            for version in index.get("versions", []):
                print(
                    f"  {version['version']} {version['storage_mode']} {version['endpoint_id']} {version['status']}"
                )
    if count == 0:
        print("prefills: 0")
    return 0


def prefill_diff(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    link, manifest = resolve_prefill(store, args.name, args.version)
    new_source = load_prefill_source(args)
    new_digest = digest_text(new_source)
    old_digest = manifest["prefill_source"]["source_digest"]
    print(f"prefill: {args.name}")
    print(f"version: {manifest['prefill_source']['version']}")
    print(f"old digest: {old_digest}")
    print(f"new digest: {new_digest}")
    print(f"old estimated tokens: {manifest['context']['token_count']}")
    print(f"new estimated tokens: {estimate_tokens(new_source)}")
    if old_digest == new_digest:
        print("status: unchanged")
        return 0
    print("status: changed; create a new prefill version rather than mutating this one")
    print(f"active capsule: {link['capsule_id']}")
    return 1 if args.strict else 0


def thread_start(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    endpoint = store.load_endpoint(args.endpoint)
    thread_id = args.thread_id or slugify(args.name)
    ledger_path = store.ledger_path(thread_id)
    if ledger_path.exists() and not args.force:
        print(f"Thread already exists: {thread_id}", file=sys.stderr)
        return 2

    transcript_ref = store.transcript_path(thread_id).relative_to(store.root).as_posix()
    capsules: list[JSONDict] = []
    active_capsule_id = None
    fallback = {
        "mode": "full_replay",
        "replay_start_token": 0,
        "reason": "No checkpoint has been created yet.",
    }
    notes = [
        "Thread ledger created by capsule_cli.py. Transcript is canonical; capsules are acceleration."
    ]

    if args.prefill:
        prefill_link, prefill_manifest = resolve_prefill(store, args.prefill, args.prefill_version, endpoint["endpoint_id"])
        active_capsule_id = prefill_link["capsule_id"]
        prefill_end = int(prefill_manifest["context"]["token_end"])
        capsules.append(
            {
                "capsule_id": prefill_link["capsule_id"],
                "manifest_ref": prefill_link["manifest_ref"],
                "kind": prefill_manifest["kind"],
                "parent_capsule_id": None,
                "token_start": 0,
                "token_end": prefill_end,
                "endpoint_id": endpoint["endpoint_id"],
                "status": "active",
            }
        )
        if prefill_manifest["storage"]["mode"] == "soft":
            fallback = {
                "mode": "full_replay",
                "replay_start_token": 0,
                "reason": f"Prefill {args.prefill} is soft-only and must be replayed from source.",
            }
        else:
            fallback = {
                "mode": "replay_from_checkpoint",
                "replay_start_token": prefill_end,
                "reason": f"Restore prefill {args.prefill}, then append transcript messages after token {prefill_end}.",
            }
        notes.append(f"Started from prefill capsule {active_capsule_id}.")

    ledger: JSONDict = {
        "schema_version": "0.1",
        "thread_id": thread_id,
        "display_name": args.name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "endpoint_id": endpoint["endpoint_id"],
        "workspace_ref": args.workspace,
        "transcript_ref": transcript_ref,
        "active_capsule_id": active_capsule_id,
        "capsules": capsules,
        "open_diffs": [],
        "fallback": fallback,
        "notes": notes,
    }
    write_json(ledger_path, ledger)
    store.transcript_path(thread_id).parent.mkdir(parents=True, exist_ok=True)
    store.transcript_path(thread_id).touch(exist_ok=True)
    print(f"started thread: {thread_id}")
    print(f"ledger: {ledger_path}")
    return 0


def load_content(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.content is not None:
        return args.content
    raise ValueError("Provide --content or --file")


def transcript_token_end(rows: list[JSONDict]) -> int:
    if not rows:
        return 0
    return int(rows[-1].get("token_end", 0))


def active_capsule_token_end(ledger: JSONDict) -> int:
    active = ledger.get("active_capsule_id")
    if active is None:
        return 0
    for item in ledger.get("capsules", []):
        if item["capsule_id"] == active:
            return int(item.get("token_end", 0))
    return 0


def transcript_or_capsule_token_end(rows: list[JSONDict], ledger: JSONDict) -> int:
    if rows:
        return transcript_token_end(rows)
    return active_capsule_token_end(ledger)


def thread_append(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    ledger = store.load_ledger(args.thread)
    transcript_path = store.transcript_path(args.thread)
    rows = read_jsonl(transcript_path)
    content = load_content(args)
    token_start = transcript_or_capsule_token_end(rows, ledger)
    token_count = estimate_tokens(content)
    token_end = token_start + token_count
    message_id = f"msg_{len(rows) + 1:04d}"
    payload: JSONDict = {
        "schema_version": "0.1",
        "message_id": message_id,
        "created_at": now_iso(),
        "role": args.role,
        "content": content,
        "content_digest": digest_text(content),
        "token_start": token_start,
        "token_end": token_end,
        "token_count_estimated": token_count,
    }
    append_jsonl(transcript_path, payload)

    ledger["updated_at"] = now_iso()
    active = ledger.get("active_capsule_id")
    existing_diffs = ledger.get("open_diffs", [])
    if existing_diffs and existing_diffs[0].get("after_capsule_id") == active:
        diff_start = int(existing_diffs[0].get("token_start", token_start))
    elif active is None:
        diff_start = 0
    else:
        diff_start = token_start

    ledger["open_diffs"] = [
        {
            "after_capsule_id": active,
            "token_start": diff_start,
            "token_end": token_end,
            "transcript_ref": ledger["transcript_ref"],
        }
    ]
    if active is None:
        ledger["fallback"] = {
            "mode": "full_replay",
            "replay_start_token": 0,
            "reason": "Thread has transcript content but no checkpoint yet.",
        }
    write_json(store.ledger_path(args.thread), ledger)
    print(f"appended {message_id} to {args.thread}: tokens {token_start}..{token_end} estimated")
    return 0


def build_segments(rows: list[JSONDict]) -> list[JSONDict]:
    segments: list[JSONDict] = []
    for row in rows:
        segments.append(
            {
                "segment_id": row["message_id"],
                "source": row["role"] if row["role"] in {"system", "user", "assistant", "tool"} else "metadata",
                "role": row["role"],
                "token_start": row["token_start"],
                "token_end": row["token_end"],
                "digest": row["content_digest"],
            }
        )
    return segments


def find_capsule_link(ledger: JSONDict, capsule_id: str | None) -> JSONDict | None:
    if capsule_id is None:
        return None
    for item in ledger.get("capsules", []):
        if item["capsule_id"] == capsule_id:
            return item
    return None


def mark_hard_capsule_restore_failed(store: Store, ledger: JSONDict, link: JSONDict, manifest: JSONDict, error_text: str) -> None:
    lifecycle = manifest_lifecycle(manifest)
    lifecycle["last_restore_failed_at"] = now_iso()
    lifecycle["last_restore_error"] = error_text
    notes = manifest.setdefault("notes", [])
    if isinstance(notes, list):
        notes.append("Hard snapshot restore failed; transcript replay remains canonical.")
    write_json(store.root / link["manifest_ref"], manifest)

    ledger_link = find_capsule_link(ledger, str(link["capsule_id"]))
    if ledger_link is not None:
        ledger_link["status"] = "restore_failed"
        ledger_link["last_restore_failed_at"] = lifecycle["last_restore_failed_at"]

    fallback_capsule_id = None
    fallback_token = 0
    parent_id = link.get("parent_capsule_id")
    parent = find_capsule_link(ledger, str(parent_id) if parent_id else None)
    if parent is not None and str(parent.get("kind", "")).endswith("_prefill"):
        fallback_capsule_id = parent["capsule_id"]
        fallback_token = int(parent.get("token_end", 0))
    if ledger.get("active_capsule_id") == link["capsule_id"]:
        ledger["active_capsule_id"] = fallback_capsule_id

    rows = read_jsonl(store.transcript_path(str(ledger["thread_id"])))
    replay_end = transcript_or_capsule_token_end(rows, ledger)
    ledger["open_diffs"] = [
        {
            "after_capsule_id": fallback_capsule_id,
            "token_start": fallback_token,
            "token_end": replay_end,
            "transcript_ref": ledger["transcript_ref"],
        }
    ]
    ledger["fallback"] = {
        "mode": "replay_from_checkpoint" if fallback_capsule_id else "full_replay",
        "replay_start_token": fallback_token,
        "reason": f"Hard capsule {link['capsule_id']} could not be restored; replay the canonical transcript.",
    }
    ledger["updated_at"] = now_iso()
    write_json(store.ledger_path(str(ledger["thread_id"])), ledger)


def build_checkpoint_segments(store: Store, ledger: JSONDict, rows: list[JSONDict], parent_capsule_id: str | None) -> list[JSONDict]:
    segments: list[JSONDict] = []
    previous_end = 0
    parent = find_capsule_link(ledger, parent_capsule_id)
    if parent is not None:
        parent_manifest = load_manifest_ref(store, parent["manifest_ref"])
        previous_end = int(parent["token_end"])
        source = "prefill" if str(parent["kind"]).endswith("_prefill") else "metadata"
        segments.append(
            {
                "segment_id": parent["capsule_id"],
                "source": source,
                "role": "capsule_parent",
                "token_start": 0,
                "token_end": previous_end,
                "digest": parent_manifest["context"]["token_digest"],
            }
        )

    for row in rows:
        row_start = int(row["token_start"])
        row_end = int(row["token_end"])
        if row_end <= previous_end:
            continue
        if row_start != previous_end:
            raise RuntimeError(
                f"Transcript row {row['message_id']} starts at {row_start}, expected {previous_end}"
            )
        source = row["role"] if row["role"] in {"system", "user", "assistant", "tool"} else "metadata"
        segments.append(
            {
                "segment_id": row["message_id"],
                "source": source,
                "role": row["role"],
                "token_start": row_start,
                "token_end": row_end,
                "digest": row["content_digest"],
            }
        )
        previous_end = row_end
    return segments


def transcript_text(rows: list[JSONDict]) -> str:
    return "\n".join(f"{row['role']}: {row['content']}" for row in rows)


def context_digest(store: Store, ledger: JSONDict, rows: list[JSONDict], parent_capsule_id: str | None) -> str:
    pieces: list[str] = []
    parent = find_capsule_link(ledger, parent_capsule_id)
    if parent is not None:
        parent_manifest = load_manifest_ref(store, parent["manifest_ref"])
        pieces.append(parent_manifest["context"]["token_digest"])
    pieces.append(transcript_text(rows))
    return digest_text("\n".join(pieces))


def compatibility_from_endpoint(endpoint: JSONDict, token_end: int, slot_format: str) -> JSONDict:
    return {
        "runtime": endpoint["runtime"]["name"],
        "runtime_build": endpoint["runtime"]["build"],
        "model_ref": endpoint["runtime"]["model_ref"],
        "model_hash": endpoint["runtime"]["model_hash"],
        "tokenizer_hash": endpoint["runtime"]["tokenizer_hash"],
        "context_limit": max(endpoint["runtime"]["context_limit"], token_end, 1),
        "slot_format": slot_format,
    }


def checkpoint_soft(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    ledger = store.load_ledger(args.thread)
    endpoint = store.load_endpoint(ledger["endpoint_id"])
    rows = read_jsonl(store.transcript_path(args.thread))
    token_end = transcript_or_capsule_token_end(rows, ledger)
    capsule_id = args.capsule_id or f"soft_{timestamp_id()}"
    manifest_ref = (Path("threads") / args.thread / "manifests" / f"{capsule_id}.json").as_posix()
    parent_capsule_id = ledger.get("active_capsule_id")

    compatibility = compatibility_from_endpoint(endpoint, token_end, "soft-transcript-ledger")
    manifest: JSONDict = {
        "schema_version": "0.1",
        "capsule_id": capsule_id,
        "thread_id": args.thread,
        "kind": "soft_checkpoint",
        "parent_capsule_id": parent_capsule_id,
        "endpoint_id": endpoint["endpoint_id"],
        "created_at": now_iso(),
        "expires_at": None,
        "compatibility": compatibility,
        "context": {
            "token_start": 0,
            "token_end": token_end,
            "token_count": token_end,
            "token_digest": context_digest(store, ledger, rows, parent_capsule_id),
            "segments": build_checkpoint_segments(store, ledger, rows, parent_capsule_id),
        },
        "storage": {
            "mode": "soft",
            "snapshot_ref": None,
            "snapshot_bytes": None,
            "snapshot_digest": None,
        },
        "security": {
            "sealed": False,
            "signature": None,
            "encryption": None,
        },
        "notes": [
            "Soft checkpoint: no KV snapshot is stored. Restore requires transcript replay."
        ],
    }
    write_json(store.manifests_dir(args.thread) / f"{capsule_id}.json", manifest)

    ledger["updated_at"] = now_iso()
    ledger["active_capsule_id"] = capsule_id
    ledger["capsules"].append(
        {
            "capsule_id": capsule_id,
            "manifest_ref": manifest_ref,
            "kind": "soft_checkpoint",
            "parent_capsule_id": parent_capsule_id,
            "token_start": 0,
            "token_end": token_end,
            "endpoint_id": endpoint["endpoint_id"],
            "status": "active",
        }
    )
    for item in ledger["capsules"]:
        if item["capsule_id"] != capsule_id and item.get("status") == "active":
            item["status"] = "superseded"
    ledger["open_diffs"] = []
    ledger["fallback"] = {
        "mode": "full_replay",
        "replay_start_token": 0,
        "reason": "Latest checkpoint is soft-only and has no restorable KV snapshot.",
    }
    write_json(store.ledger_path(args.thread), ledger)
    print(f"wrote soft checkpoint: {capsule_id}")
    return 0


def update_ledger_for_capsule(
    store: Store,
    ledger: JSONDict,
    capsule_id: str,
    manifest_ref: str,
    kind: str,
    parent_capsule_id: str | None,
    token_end: int,
    endpoint_id: str,
    fallback_mode: str,
    fallback_reason: str,
) -> None:
    ledger["updated_at"] = now_iso()
    ledger["active_capsule_id"] = capsule_id
    ledger["capsules"].append(
        {
            "capsule_id": capsule_id,
            "manifest_ref": manifest_ref,
            "kind": kind,
            "parent_capsule_id": parent_capsule_id,
            "token_start": 0,
            "token_end": token_end,
            "endpoint_id": endpoint_id,
            "status": "active",
        }
    )
    for item in ledger["capsules"]:
        if item["capsule_id"] != capsule_id and item.get("status") == "active":
            item["status"] = "superseded"
    ledger["open_diffs"] = []
    ledger["fallback"] = {
        "mode": fallback_mode,
        "replay_start_token": token_end if fallback_mode == "replay_from_checkpoint" else 0,
        "reason": fallback_reason,
    }
    write_json(store.ledger_path(ledger["thread_id"]), ledger)


def snapshot_metadata(path: Path, save_response: JSONDict) -> tuple[int | None, str | None]:
    if path.exists():
        return path.stat().st_size, digest_file(path)
    for key in ("n_written", "snapshot_bytes", "bytes"):
        value = save_response.get(key)
        if isinstance(value, int) and value >= 0:
            return value, None
    return None, None


def create_hard_checkpoint(
    store: Store,
    thread_id: str,
    slot_id: int,
    capsule_id: str | None,
    timeout: float,
    runtime_filename: str | None,
) -> str:
    ledger = store.load_ledger(thread_id)
    endpoint = store.load_endpoint(ledger["endpoint_id"])
    if not endpoint.get("capabilities", {}).get("slot_save_restore", False):
        raise RuntimeError("Endpoint does not advertise slot_save_restore. Run endpoint doctor or use --soft.")

    rows = read_jsonl(store.transcript_path(thread_id))
    token_end = transcript_or_capsule_token_end(rows, ledger)
    capsule_id = capsule_id or f"cap_{timestamp_id()}"
    snapshot_path = store.snapshots_dir(thread_id) / f"{capsule_id}.bin"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_snapshot_ref = runtime_filename or str(snapshot_path.resolve())

    save_response, save_ms = slot_action(endpoint, slot_id, "save", runtime_snapshot_ref, timeout)
    snapshot_bytes, snapshot_digest = snapshot_metadata(snapshot_path, save_response)
    parent_capsule_id = ledger.get("active_capsule_id")
    manifest_ref = (Path("threads") / thread_id / "manifests" / f"{capsule_id}.json").as_posix()

    manifest: JSONDict = {
        "schema_version": "0.1",
        "capsule_id": capsule_id,
        "thread_id": thread_id,
        "kind": "thread_checkpoint",
        "parent_capsule_id": parent_capsule_id,
        "endpoint_id": endpoint["endpoint_id"],
        "created_at": now_iso(),
        "expires_at": None,
        "compatibility": compatibility_from_endpoint(endpoint, token_end, "llama.cpp-slot-save-restore"),
        "context": {
            "token_start": 0,
            "token_end": token_end,
            "token_count": token_end,
            "token_digest": context_digest(store, ledger, rows, parent_capsule_id),
            "segments": build_checkpoint_segments(store, ledger, rows, parent_capsule_id),
        },
        "storage": {
            "mode": "local_file",
            "snapshot_ref": store.relative_ref(snapshot_path),
            "runtime_snapshot_ref": runtime_snapshot_ref,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_digest": snapshot_digest,
        },
        "security": {
            "sealed": False,
            "signature": None,
            "encryption": None,
        },
        "notes": [
            f"Hard checkpoint saved from slot {slot_id}.",
            f"llama.cpp save client duration: {save_ms} ms.",
        ],
    }
    write_json(store.manifests_dir(thread_id) / f"{capsule_id}.json", manifest)
    update_ledger_for_capsule(
        store,
        ledger,
        capsule_id,
        manifest_ref,
        "thread_checkpoint",
        parent_capsule_id,
        token_end,
        endpoint["endpoint_id"],
        "replay_from_checkpoint",
        "Restore the hard capsule, then append transcript diffs after this checkpoint. If restore fails, replay the canonical transcript.",
    )
    return capsule_id


def checkpoint_hard(args: argparse.Namespace) -> int:
    capsule_id = create_hard_checkpoint(
        Store(args.state_dir),
        args.thread,
        args.slot,
        args.capsule_id,
        args.timeout,
        args.runtime_filename,
    )
    print(f"wrote hard checkpoint: {capsule_id}")
    return 0


def load_manifest_ref(store: Store, manifest_ref: str) -> JSONDict:
    return read_json(store.root / manifest_ref)


def find_latest_restorable_manifest(store: Store, ledger: JSONDict, capsule_id: str | None) -> tuple[JSONDict, JSONDict]:
    links = ledger.get("capsules", [])
    if capsule_id is not None:
        links = [item for item in links if item["capsule_id"] == capsule_id]
    else:
        links = list(reversed(links))
    for link in links:
        if link.get("status") in {"missing", "restore_failed"}:
            continue
        manifest = load_manifest_ref(store, link["manifest_ref"])
        lifecycle = manifest.get("lifecycle", {})
        if isinstance(lifecycle, dict) and lifecycle.get("snapshot_present") is False:
            continue
        if manifest.get("storage", {}).get("mode") != "soft":
            return link, manifest
    raise RuntimeError("No restorable hard capsule found for this thread")


def assert_manifest_compatible(manifest: JSONDict, endpoint: JSONDict) -> None:
    if manifest["endpoint_id"] != endpoint["endpoint_id"]:
        raise RuntimeError("Capsule endpoint_id does not match thread endpoint")
    compatibility = manifest["compatibility"]
    runtime = endpoint["runtime"]
    checks = [
        ("model_hash", compatibility["model_hash"], runtime["model_hash"]),
        ("tokenizer_hash", compatibility["tokenizer_hash"], runtime["tokenizer_hash"]),
    ]
    for label, left, right in checks:
        if left != "unknown" and right != "unknown" and left != right:
            raise RuntimeError(f"Capsule {label} mismatch: {left} != {right}")
    if compatibility["context_limit"] > runtime["context_limit"]:
        raise RuntimeError("Capsule context_limit exceeds endpoint context_limit")


def diff_messages_after(rows: list[JSONDict], token_end: int) -> list[JSONDict]:
    messages: list[JSONDict] = []
    for row in rows:
        if int(row.get("token_end", 0)) <= token_end:
            continue
        messages.append({"role": row["role"], "content": row["content"]})
    return messages


def replay_messages_from_active_context(store: Store, ledger: JSONDict, rows: list[JSONDict]) -> list[JSONDict]:
    messages: list[JSONDict] = []
    active = find_capsule_link(ledger, ledger.get("active_capsule_id"))
    if active is not None and str(active.get("kind", "")).endswith("_prefill"):
        manifest = load_manifest_ref(store, active["manifest_ref"])
        source_ref = manifest.get("prefill_source", {}).get("source_ref")
        if source_ref:
            source_path = store.root / str(source_ref)
            if source_path.exists():
                messages.append({"role": "system", "content": source_path.read_text(encoding="utf-8")})
    for row in rows:
        messages.append({"role": row["role"], "content": row["content"]})
    return messages


def resume_thread(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    ledger = store.load_ledger(args.thread)
    endpoint = store.load_endpoint(ledger["endpoint_id"])
    link, manifest = find_latest_restorable_manifest(store, ledger, args.capsule_id)
    assert_manifest_compatible(manifest, endpoint)

    storage = manifest["storage"]
    runtime_snapshot_ref = storage.get("runtime_snapshot_ref") or storage.get("snapshot_ref")
    if not runtime_snapshot_ref:
        raise RuntimeError("Capsule has no runtime snapshot reference")

    try:
        restore_response, restore_ms = slot_action(endpoint, args.slot, "restore", runtime_snapshot_ref, args.timeout)
    except Exception as exc:  # noqa: BLE001 - restore failure should degrade to replay fallback.
        error_text = str(exc)
        print(f"warning: restore failed for {link['capsule_id']}: {error_text}")
        mark_hard_capsule_restore_failed(store, ledger, link, manifest, error_text)
        ledger = store.load_ledger(args.thread)
        rows = read_jsonl(store.transcript_path(args.thread))
        replay_start = int(ledger.get("fallback", {}).get("replay_start_token", 0))
        print(f"fallback: {ledger['fallback']['mode']} from token {replay_start}")
        if not args.append_diff:
            print("restore failed before append; replay the canonical transcript before continuing")
            return 0

        messages = replay_messages_from_active_context(store, ledger, rows)
        if messages:
            response, replay_ms = chat_completion(
                endpoint,
                args.slot,
                messages,
                args.max_tokens,
                args.temperature,
                args.timeout,
                args.chat_path,
                cache_prompt=False,
            )
            print(f"replayed {len(messages)} canonical messages ({replay_ms} ms)")
            finish_reason = response.get("choices", [{}])[0].get("finish_reason") if isinstance(response.get("choices"), list) else None
            if finish_reason:
                print(f"finish_reason: {finish_reason}")
        else:
            print("no transcript messages to replay")
        capsule_id = create_hard_checkpoint(
            store,
            args.thread,
            args.slot,
            f"fallback_{timestamp_id()}",
            args.timeout,
            None,
        )
        print(f"saved fallback checkpoint: {capsule_id}")
        return 0

    print(f"restored {link['capsule_id']} into slot {args.slot} ({restore_ms} ms)")

    if args.append_diff:
        rows = read_jsonl(store.transcript_path(args.thread))
        messages = diff_messages_after(rows, int(manifest["context"]["token_end"]))
        if messages:
            response, completion_ms = chat_completion(
                endpoint,
                args.slot,
                messages,
                args.max_tokens,
                args.temperature,
                args.timeout,
                args.chat_path,
            )
            print(f"appended {len(messages)} diff messages ({completion_ms} ms)")
            finish_reason = response.get("choices", [{}])[0].get("finish_reason") if isinstance(response.get("choices"), list) else None
            if finish_reason:
                print(f"finish_reason: {finish_reason}")
        else:
            print("no transcript diff to append")
    else:
        open_diffs = ledger.get("open_diffs", [])
        if open_diffs:
            diff = open_diffs[0]
            print(f"pending diff tokens: {diff['token_start']}..{diff['token_end']} (use --append-diff)")

    if restore_response:
        detail = restore_response.get("timings") or restore_response
        print(f"restore response: {json.dumps(detail, sort_keys=True)}")
    return 0


def shutdown_thread(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    ledger = store.load_ledger(args.thread)
    if not ledger.get("open_diffs") and not args.force:
        print("no open diff recorded; use --force to checkpoint anyway")
        return 0
    capsule_id = create_hard_checkpoint(
        store,
        args.thread,
        args.slot,
        args.capsule_id or f"shutdown_{timestamp_id()}",
        args.timeout,
        args.runtime_filename,
    )
    print(f"saved shutdown checkpoint: {capsule_id}")
    return 0


def safe_zip_name(path: str) -> str:
    normalized = Path(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise RuntimeError(f"Unsafe bundle path: {path}")
    return normalized.as_posix()


def add_text_to_zip(bundle: zipfile.ZipFile, name: str, content: str) -> None:
    bundle.writestr(safe_zip_name(name), content)


def add_bytes_to_zip(bundle: zipfile.ZipFile, name: str, content: bytes) -> None:
    bundle.writestr(safe_zip_name(name), content)


def add_file_to_zip(bundle: zipfile.ZipFile, source: Path, arcname: str) -> bool:
    if not source.exists() or not source.is_file():
        return False
    bundle.write(source, safe_zip_name(arcname))
    return True


def pretty_json_bytes(data: JSONDict | list[JSONDict]) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def text_bytes(value: str) -> bytes:
    return value.encode("utf-8")


def add_export_data(entries: list[BundleEntry], name: str, content: bytes) -> None:
    entries.append(BundleEntry(safe_zip_name(name), data=content))


def add_export_file(entries: list[BundleEntry], source: Path, arcname: str) -> bool:
    if not source.exists() or not source.is_file():
        return False
    entries.append(BundleEntry(safe_zip_name(arcname), source=source))
    return True


def build_export_plan(
    store: Store,
    ledger: JSONDict,
    thread_id: str,
    transcript_content: str,
    include_snapshots: bool,
    redact_transcript: bool,
) -> ExportBundlePlan:
    entries: list[BundleEntry] = []
    capsule_index: list[JSONDict] = []
    omitted_snapshots: list[str] = []
    included_files: list[str] = []

    add_export_data(entries, "thread-ledger.json", pretty_json_bytes(ledger))
    add_export_data(entries, "transcript.jsonl", text_bytes(transcript_content))

    state_ledger_ref = f"threads/{thread_id}/thread-ledger.json"
    add_export_data(entries, state_ledger_ref, pretty_json_bytes(ledger))
    included_files.append(state_ledger_ref)

    state_transcript_ref = ledger["transcript_ref"]
    add_export_data(entries, state_transcript_ref, text_bytes(transcript_content))
    included_files.append(state_transcript_ref)

    endpoint_ref = f"endpoints/{ledger['endpoint_id']}.json"
    if add_export_file(entries, store.root / endpoint_ref, endpoint_ref):
        included_files.append(endpoint_ref)

    for link in ledger.get("capsules", []):
        manifest_ref = link["manifest_ref"]
        manifest_path = store.root / manifest_ref
        if not manifest_path.exists():
            capsule_index.append({**link, "included_manifest": False})
            continue
        manifest = read_json(manifest_path)
        add_export_file(entries, manifest_path, manifest_ref)
        included_files.append(manifest_ref)

        prefill_source = manifest.get("prefill_source")
        if prefill_source and not redact_transcript:
            source_ref = prefill_source.get("source_ref")
            if source_ref and add_export_file(entries, store.root / source_ref, source_ref):
                included_files.append(source_ref)

        snapshot_included = False
        snapshot_ref = manifest.get("storage", {}).get("snapshot_ref")
        if snapshot_ref:
            if include_snapshots:
                snapshot_included = add_export_file(entries, store.root / snapshot_ref, snapshot_ref)
                if snapshot_included:
                    included_files.append(snapshot_ref)
            else:
                omitted_snapshots.append(snapshot_ref)

        capsule_index.append(
            {
                **link,
                "included_manifest": True,
                "included_snapshot": snapshot_included,
                "snapshot_ref": snapshot_ref,
            }
        )

    add_export_data(entries, "capsule-index.json", pretty_json_bytes(capsule_index))
    return ExportBundlePlan(
        entries=entries,
        included_files=sorted(set(included_files)),
        omitted_snapshots=sorted(set(omitted_snapshots)),
        payload_bytes=sum(entry.size for entry in entries),
    )


def write_export_plan(out_path: Path, plan: ExportBundlePlan) -> None:
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for entry in plan.entries:
            if entry.data is not None:
                add_bytes_to_zip(bundle, entry.name, entry.data)
            elif entry.source is not None:
                add_file_to_zip(bundle, entry.source, entry.name)


def print_export_plan(out_path: Path, thread_id: str, include_snapshots: bool, plan: ExportBundlePlan, dry_run: bool) -> None:
    label = "would export bundle" if dry_run else "export plan"
    print(f"{label}: {out_path}")
    print(f"thread: {thread_id}")
    print(f"entries: {len(plan.entries)}")
    print(f"estimated payload bytes: {plan.payload_bytes}")
    print(f"estimated payload size: {format_bytes(plan.payload_bytes)}")
    print(f"snapshots included: {include_snapshots}")
    if plan.omitted_snapshots:
        print(f"omitted snapshots: {len(plan.omitted_snapshots)}")


def zip_payload_digests(bundle: zipfile.ZipFile) -> dict[str, str]:
    digests: dict[str, str] = {}
    seen: set[str] = set()
    for item in bundle.infolist():
        name = safe_zip_name(item.filename)
        if name in seen:
            raise RuntimeError(f"Duplicate bundle entry: {name}")
        seen.add(name)
        if name == "manifest.json":
            continue
        digests[name] = digest_bytes(bundle.read(item.filename))
    return dict(sorted(digests.items()))


def bundle_file_digests(bundle_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(bundle_path, "r") as bundle:
        return zip_payload_digests(bundle)


def canonical_json_bytes(data: JSONDict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def read_signature_key(key_file: Path | None, key_env: str | None) -> bytes | None:
    if key_file and key_env:
        raise RuntimeError("Use only one signature key source: --signature-key-file or --signature-key-env")
    key: bytes | None = None
    if key_file:
        key = key_file.read_bytes().strip()
    elif key_env:
        value = os.environ.get(key_env)
        if value is None:
            raise RuntimeError(f"Signature key environment variable is not set: {key_env}")
        key = value.encode("utf-8")
    if key is not None and not key:
        raise RuntimeError("Signature key is empty")
    return key


def signature_payload(manifest: JSONDict) -> bytes:
    payload = json.loads(json.dumps(manifest))
    integrity = payload.setdefault("integrity", {})
    if not isinstance(integrity, dict):
        raise RuntimeError("Bundle manifest integrity must be an object")
    integrity["signature"] = None
    return canonical_json_bytes(payload)


def hmac_signature(manifest: JSONDict, key: bytes) -> str:
    raw = hmac.new(key, signature_payload(manifest), hashlib.sha256).digest()
    return "base64url:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_bundle_signature(manifest: JSONDict, key: bytes, key_id: str | None) -> JSONDict:
    return {
        "algorithm": "hmac-sha256",
        "key_id": key_id,
        "digest": hmac_signature(manifest, key),
    }


def verify_bundle_signature(manifest: JSONDict, key: bytes) -> JSONDict:
    integrity = manifest.get("integrity", {})
    if not isinstance(integrity, dict):
        raise RuntimeError("Bundle manifest integrity must be an object")
    signature = integrity.get("signature")
    if not isinstance(signature, dict):
        raise RuntimeError("Bundle signature is not present")
    if signature.get("algorithm") != "hmac-sha256":
        raise RuntimeError(f"Unsupported bundle signature algorithm: {signature.get('algorithm')}")
    expected = hmac_signature(manifest, key)
    actual = str(signature.get("digest", ""))
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("Bundle signature verification failed")
    return {
        "algorithm": signature["algorithm"],
        "key_id": signature.get("key_id"),
        "verified": True,
    }


def verify_bundle_integrity(
    bundle_path: Path,
    signature_key: bytes | None = None,
    require_signature: bool = False,
) -> JSONDict:
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_path}")
    with zipfile.ZipFile(bundle_path, "r") as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        actual = zip_payload_digests(bundle)
    expected = manifest.get("file_digests")
    if expected is None:
        return {
            "verified": False,
            "reason": "bundle has no file_digests index",
            "thread_id": manifest.get("thread_id"),
            "entries": len(actual),
            "signature": "not_checked",
        }
    if not isinstance(expected, dict):
        raise RuntimeError("Bundle manifest file_digests must be an object")
    expected_map = {safe_zip_name(str(key)): str(value) for key, value in expected.items()}
    missing = sorted(set(expected_map) - set(actual))
    extra = sorted(set(actual) - set(expected_map))
    mismatched = sorted(name for name in expected_map if name in actual and expected_map[name] != actual[name])
    if missing or extra or mismatched:
        raise RuntimeError(
            "Bundle digest verification failed: "
            f"missing={missing}, extra={extra}, mismatched={mismatched}"
        )
    integrity = manifest.get("integrity", {})
    signature = integrity.get("signature") if isinstance(integrity, dict) else None
    signature_status: JSONDict = {
        "status": "absent" if signature is None else "present_unchecked",
        "algorithm": signature.get("algorithm") if isinstance(signature, dict) else None,
        "key_id": signature.get("key_id") if isinstance(signature, dict) else None,
    }
    if require_signature and signature_key is None:
        raise RuntimeError("A signature key is required when --require-signature is set")
    if signature_key is not None:
        signature_status = verify_bundle_signature(manifest, signature_key)
        signature_status["status"] = "verified"
    elif require_signature:
        raise RuntimeError("Bundle signature is required")

    return {
        "verified": True,
        "thread_id": manifest.get("thread_id"),
        "entries": len(actual),
        "algorithm": "sha256",
        "signature": signature_status,
    }


def bundle_json(bundle: zipfile.ZipFile, name: str) -> JSONDict:
    data = json.loads(bundle.read(name).decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Bundle entry is not a JSON object: {name}")
    return data


def nested_value(payload: JSONDict, path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def import_compatibility_warnings(store: Store, bundle: zipfile.ZipFile) -> list[str]:
    warnings: list[str] = []
    endpoint_names = sorted(
        safe_zip_name(item.filename)
        for item in bundle.infolist()
        if safe_zip_name(item.filename).startswith("endpoints/") and safe_zip_name(item.filename).endswith(".json")
    )
    compare_fields = [
        "type",
        "base_url",
        "runtime.name",
        "runtime.build",
        "runtime.model_ref",
        "runtime.model_hash",
        "runtime.tokenizer_hash",
        "runtime.context_limit",
        "slot_api.slot_field",
    ]
    for name in endpoint_names:
        incoming = bundle_json(bundle, name)
        endpoint_id = str(incoming.get("endpoint_id") or Path(name).stem)
        local_path = store.endpoint_path(endpoint_id)
        if not local_path.exists():
            continue
        local = read_json(local_path)
        differences = []
        for field in compare_fields:
            local_value = nested_value(local, field)
            incoming_value = nested_value(incoming, field)
            if local_value != incoming_value:
                differences.append(f"{field}: local={local_value!r} bundle={incoming_value!r}")
        if differences:
            warnings.append(
                f"endpoint {endpoint_id} differs from local endpoint ({'; '.join(differences)}); "
                "imported endpoint metadata will overwrite the local endpoint record"
            )
    return warnings


def export_bundle(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    ledger = store.load_ledger(args.thread)
    out_path = args.out.resolve()
    transcript_path = store.transcript_path(args.thread)
    transcript_content = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
    if args.redact_transcript:
        transcript_content = ""

    plan = build_export_plan(
        store,
        ledger,
        args.thread,
        transcript_content,
        bool(args.include_snapshots),
        bool(args.redact_transcript),
    )
    print_export_plan(out_path, args.thread, bool(args.include_snapshots), plan, bool(getattr(args, "dry_run", False)))
    if getattr(args, "dry_run", False):
        return 0

    if out_path.exists() and not args.force:
        raise FileExistsError(f"Bundle already exists: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bundle_manifest: JSONDict = {
        "schema_version": "0.1",
        "bundle_type": "session-capsules.scap",
        "created_at": now_iso(),
        "thread_id": args.thread,
        "export_mode": "with-local-snapshots" if args.include_snapshots else "ledger-only",
        "redacted_transcript": args.redact_transcript,
        "includes_snapshots": args.include_snapshots,
        "notes": [
            "Model weights are never included in .scap bundles.",
            "Hard snapshots are included only when includes_snapshots is true.",
        ],
    }

    write_export_plan(out_path, plan)
    bundle_manifest["included_files"] = plan.included_files
    bundle_manifest["omitted_snapshots"] = plan.omitted_snapshots
    signature_key = read_signature_key(getattr(args, "signature_key_file", None), getattr(args, "signature_key_env", None))

    bundle_manifest["integrity"] = {
        "file_digest_algorithm": "sha256",
        "signature": None,
        "encryption": None,
        "notes": [
            "file_digests cover every zip entry except manifest.json.",
            "HMAC signatures are optional and use an external key supplied at export time.",
            "Encryption is not implemented in this local bundle format yet.",
        ],
    }
    bundle_manifest["file_digests"] = bundle_file_digests(out_path)
    if signature_key is not None:
        bundle_manifest["integrity"]["signature"] = build_bundle_signature(
            bundle_manifest,
            signature_key,
            getattr(args, "signature_key_id", None),
        )
    with zipfile.ZipFile(out_path, "a", compression=zipfile.ZIP_DEFLATED) as bundle:
        add_text_to_zip(bundle, "manifest.json", json.dumps(bundle_manifest, indent=2) + "\n")

    print(f"exported bundle: {out_path}")
    print(f"thread: {args.thread}")
    print(f"snapshots included: {args.include_snapshots}")
    print(f"bundle bytes: {out_path.stat().st_size}")
    print(f"bundle size: {format_bytes(out_path.stat().st_size)}")
    return 0


def import_bundle(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    bundle_path = args.bundle.resolve()
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_path}")

    signature_key = read_signature_key(getattr(args, "signature_key_file", None), getattr(args, "signature_key_env", None))
    integrity = verify_bundle_integrity(bundle_path, signature_key, bool(getattr(args, "require_signature", False)))
    if not integrity["verified"]:
        print(f"warning: {integrity['reason']}")

    with zipfile.ZipFile(bundle_path, "r") as bundle:
        bundle_manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        thread_id = args.thread_id or bundle_manifest["thread_id"]
        if args.thread_id is not None and args.thread_id != bundle_manifest["thread_id"]:
            raise RuntimeError("Import thread-id override is not implemented yet because manifest refs are path-bound")
        target_ledger = store.ledger_path(thread_id)
        if target_ledger.exists() and not args.force:
            raise FileExistsError(f"Thread already exists: {thread_id}")
        compatibility_warnings = import_compatibility_warnings(store, bundle)

        extracted = 0
        for item in bundle.infolist():
            name = safe_zip_name(item.filename)
            if not (name.startswith("endpoints/") or name.startswith("prefills/") or name.startswith("threads/")):
                continue
            parts = Path(name).parts
            if parts[0] == "threads" and len(parts) > 1 and parts[1] != bundle_manifest["thread_id"]:
                raise RuntimeError(f"Unexpected thread path in bundle: {name}")
            target = store.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(item.filename))
            extracted += 1

    print(f"imported bundle: {bundle_path}")
    print(f"thread: {thread_id}")
    print(f"files: {extracted}")
    for warning in compatibility_warnings:
        print(f"warning: {warning}")
    if bundle_manifest.get("omitted_snapshots"):
        print(f"warning: omitted snapshots: {len(bundle_manifest['omitted_snapshots'])}")
    if bundle_manifest.get("redacted_transcript"):
        print("warning: transcript was redacted in this bundle")
    return 0


def verify_bundle(args: argparse.Namespace) -> int:
    bundle_path = args.bundle.resolve()
    signature_key = read_signature_key(args.signature_key_file, args.signature_key_env)
    result = verify_bundle_integrity(bundle_path, signature_key, args.require_signature)
    print(f"bundle: {bundle_path}")
    print(f"thread: {result.get('thread_id')}")
    print(f"verified: {'yes' if result['verified'] else 'no'}")
    print(f"entries: {result.get('entries')}")
    if result.get("algorithm"):
        print(f"algorithm: {result['algorithm']}")
    signature = result.get("signature")
    if isinstance(signature, dict):
        print(f"signature: {signature.get('status')}")
        if signature.get("algorithm"):
            print(f"signature algorithm: {signature['algorithm']}")
        if signature.get("key_id"):
            print(f"signature key id: {signature['key_id']}")
    if result.get("reason"):
        print(f"reason: {result['reason']}")
    return 0 if result["verified"] else 1


def config_init(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    if store.config_path.exists() and not args.force:
        print(f"config already exists: {store.config_path}")
        return 0
    write_config(store, load_config(store))
    print(f"wrote config: {store.config_path}")
    return 0


def config_show(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    config = load_config(store)
    if args.key:
        print(get_nested(config, args.key))
    else:
        print(json.dumps(config, indent=2))
    return 0


def config_set(args: argparse.Namespace) -> int:
    if args.key not in CONFIG_SETTERS:
        allowed = ", ".join(sorted(CONFIG_SETTERS))
        raise RuntimeError(f"Unsupported config key: {args.key}. Allowed: {allowed}")
    store = Store(args.state_dir)
    config = load_config(store)
    set_nested(config, args.key, parse_config_value(args.key, args.value))
    write_config(store, config)
    print(f"set {args.key}={get_nested(config, args.key)}")
    return 0


def resolve_snapshot_path(store: Store, snapshot_ref: str) -> Path:
    path = Path(snapshot_ref)
    if path.is_absolute():
        return path
    return store.root / path


def manifest_lifecycle(manifest: JSONDict) -> JSONDict:
    lifecycle = manifest.setdefault("lifecycle", {})
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        manifest["lifecycle"] = lifecycle
    return lifecycle


def iter_thread_ledgers(store: Store) -> list[tuple[Path, JSONDict]]:
    if not store.threads_dir.exists():
        return []
    rows: list[tuple[Path, JSONDict]] = []
    for ledger_path in sorted(store.threads_dir.glob("*/thread-ledger.json")):
        rows.append((ledger_path, read_json(ledger_path)))
    return rows


def hard_snapshot_records(store: Store, config: JSONDict) -> list[JSONDict]:
    records: list[JSONDict] = []
    keep_latest = int(config["storage"].get("keep_latest_per_thread", 1))

    for ledger_path, ledger in iter_thread_ledgers(store):
        hard_records: list[JSONDict] = []
        for link in ledger.get("capsules", []):
            manifest_path = store.root / link["manifest_ref"]
            if not manifest_path.exists():
                continue
            manifest = read_json(manifest_path)
            storage = manifest.get("storage", {})
            snapshot_ref = storage.get("snapshot_ref")
            if storage.get("mode") != "local_file" or not snapshot_ref:
                continue
            snapshot_path = resolve_snapshot_path(store, snapshot_ref)
            lifecycle = manifest.get("lifecycle", {})
            pinned = bool(link.get("pinned") or lifecycle.get("pinned"))
            active = link["capsule_id"] == ledger.get("active_capsule_id")
            record: JSONDict = {
                "kind": "thread",
                "thread_id": ledger["thread_id"],
                "capsule_id": link["capsule_id"],
                "manifest_ref": link["manifest_ref"],
                "manifest_path": manifest_path,
                "ledger_path": ledger_path,
                "snapshot_ref": snapshot_ref,
                "snapshot_path": snapshot_path,
                "size": snapshot_path.stat().st_size if snapshot_path.exists() else int(storage.get("snapshot_bytes") or 0),
                "exists": snapshot_path.exists(),
                "created_at": manifest.get("created_at", ""),
                "pinned": pinned,
                "active": active,
                "latest_protected": False,
                "protected": pinned or active,
                "protect_reason": "pinned" if pinned else ("active" if active else ""),
            }
            hard_records.append(record)
            records.append(record)

        protected_latest = [item for item in hard_records if item["exists"]][-keep_latest:] if keep_latest > 0 else []
        for record in protected_latest:
            record["latest_protected"] = True
            if not record["protected"]:
                record["protected"] = True
                record["protect_reason"] = "latest_per_thread"

    if store.prefills_dir.exists():
        for index_path in sorted(store.prefills_dir.glob("*/index.json")):
            index = read_json(index_path)
            active_version = index.get("active_version")
            for link in index.get("versions", []):
                manifest_path = store.root / link["manifest_ref"]
                if not manifest_path.exists():
                    continue
                manifest = read_json(manifest_path)
                storage = manifest.get("storage", {})
                snapshot_ref = storage.get("snapshot_ref")
                if storage.get("mode") != "local_file" or not snapshot_ref:
                    continue
                snapshot_path = resolve_snapshot_path(store, snapshot_ref)
                lifecycle = manifest.get("lifecycle", {})
                pinned = bool(link.get("pinned") or lifecycle.get("pinned"))
                active = link.get("version") == active_version
                protect_active = bool(config["storage"].get("protect_active_prefills", True))
                records.append(
                    {
                        "kind": "prefill",
                        "prefill_name": index["prefill_name"],
                        "version": link.get("version"),
                        "capsule_id": link["capsule_id"],
                        "manifest_ref": link["manifest_ref"],
                        "manifest_path": manifest_path,
                        "index_path": index_path,
                        "snapshot_ref": snapshot_ref,
                        "snapshot_path": snapshot_path,
                        "size": snapshot_path.stat().st_size if snapshot_path.exists() else int(storage.get("snapshot_bytes") or 0),
                        "exists": snapshot_path.exists(),
                        "created_at": manifest.get("created_at", ""),
                        "pinned": pinned,
                        "active": active,
                        "latest_protected": False,
                        "protected": pinned or (active and protect_active),
                        "protect_reason": "pinned" if pinned else ("active_prefill" if active and protect_active else ""),
                    }
                )

    return records


def storage_stats(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    config = load_config(store)
    records = hard_snapshot_records(store, config)
    existing = [record for record in records if record["exists"]]
    missing = [record for record in records if not record["exists"]]
    protected = [record for record in existing if record["protected"]]
    reclaimable = [record for record in existing if not record["protected"]]
    total_bytes = sum(int(record["size"]) for record in existing)
    reclaimable_bytes = sum(int(record["size"]) for record in reclaimable)
    max_bytes = parse_bytes(config["storage"]["max_bytes"])
    min_free_bytes = parse_bytes(config["storage"]["min_free_bytes"])
    disk_root = store.root if store.root.exists() else store.root.parent
    disk = shutil.disk_usage(disk_root)

    print(f"state dir: {store.root}")
    print(f"config: {store.config_path if store.config_path.exists() else '<defaults>'}")
    print(f"hard snapshots: {len(existing)} existing, {len(missing)} missing")
    print(f"snapshot bytes: {format_bytes(total_bytes)}")
    print(f"reclaimable bytes: {format_bytes(reclaimable_bytes)}")
    print(f"protected snapshots: {len(protected)}")
    print(f"storage.max_bytes: {config['storage']['max_bytes']} ({format_bytes(max_bytes)})")
    print(f"storage.min_free_bytes: {config['storage']['min_free_bytes']} ({format_bytes(min_free_bytes)})")
    print(f"disk free: {format_bytes(disk.free)}")
    return 0


def update_thread_capsule_pin(store: Store, thread_id: str, capsule_id: str | None, pinned: bool) -> str:
    ledger = store.load_ledger(thread_id)
    target_id = capsule_id or ledger.get("active_capsule_id")
    if target_id is None:
        raise RuntimeError(f"Thread has no active capsule: {thread_id}")
    link = find_capsule_link(ledger, target_id)
    if link is None:
        raise RuntimeError(f"Capsule not found: {target_id}")
    link["pinned"] = pinned
    manifest_path = store.root / link["manifest_ref"]
    manifest = read_json(manifest_path)
    lifecycle = manifest_lifecycle(manifest)
    lifecycle["pinned"] = pinned
    lifecycle["pinned_at" if pinned else "unpinned_at"] = now_iso()
    write_json(manifest_path, manifest)
    write_json(store.ledger_path(thread_id), ledger)
    return str(target_id)


def pin_capsule(args: argparse.Namespace) -> int:
    capsule_id = update_thread_capsule_pin(Store(args.state_dir), args.thread, args.capsule_id, True)
    print(f"pinned capsule: {capsule_id}")
    return 0


def unpin_capsule(args: argparse.Namespace) -> int:
    capsule_id = update_thread_capsule_pin(Store(args.state_dir), args.thread, args.capsule_id, False)
    print(f"unpinned capsule: {capsule_id}")
    return 0


def gc_plan(store: Store, config: JSONDict, max_bytes: str | None, min_free_bytes: str | None) -> tuple[list[JSONDict], int, int]:
    effective_max = parse_bytes(max_bytes or config["storage"]["max_bytes"])
    effective_min_free = parse_bytes(min_free_bytes or config["storage"]["min_free_bytes"])
    records = hard_snapshot_records(store, config)
    existing = [record for record in records if record["exists"]]
    total_bytes = sum(int(record["size"]) for record in existing)
    disk_root = store.root if store.root.exists() else store.root.parent
    free_bytes = shutil.disk_usage(disk_root).free
    target_reclaim = max(0, total_bytes - effective_max, effective_min_free - free_bytes)
    if target_reclaim <= 0:
        return [], total_bytes, target_reclaim

    candidates = sorted(
        [record for record in existing if not record["protected"]],
        key=lambda record: (record["created_at"], record["snapshot_ref"]),
    )
    selected: list[JSONDict] = []
    reclaimed = 0
    for record in candidates:
        selected.append(record)
        reclaimed += int(record["size"])
        if reclaimed >= target_reclaim:
            break
    return selected, total_bytes, target_reclaim


def mark_snapshot_deleted(store: Store, record: JSONDict) -> None:
    manifest_path = record["manifest_path"]
    manifest = read_json(manifest_path)
    lifecycle = manifest_lifecycle(manifest)
    lifecycle["snapshot_present"] = False
    lifecycle["snapshot_deleted_at"] = now_iso()
    lifecycle["snapshot_delete_reason"] = "storage_gc"
    notes = manifest.setdefault("notes", [])
    if isinstance(notes, list):
        notes.append("Hard snapshot blob deleted by storage GC; transcript replay remains canonical.")
    write_json(manifest_path, manifest)

    if record["kind"] == "thread":
        ledger = read_json(record["ledger_path"])
        link = find_capsule_link(ledger, record["capsule_id"])
        if link is not None:
            link["status"] = "missing"
        write_json(record["ledger_path"], ledger)


def gc_storage(args: argparse.Namespace) -> int:
    if args.apply and args.dry_run:
        raise RuntimeError("Use either --dry-run or --apply, not both")
    store = Store(args.state_dir)
    config = load_config(store)
    selected, total_bytes, target_reclaim = gc_plan(store, config, args.max_bytes, args.min_free_bytes)
    mode = "apply" if args.apply else "dry-run"
    print(f"mode: {mode}")
    print(f"snapshot bytes: {format_bytes(total_bytes)}")
    print(f"target reclaim: {format_bytes(target_reclaim)}")
    if not selected:
        print("gc candidates: 0")
        return 0

    print(f"gc candidates: {len(selected)}")
    for record in selected:
        print(
            f"{record['kind']} {record['capsule_id']} {format_bytes(int(record['size']))} "
            f"{record['snapshot_ref']}"
        )

    if not args.apply:
        print("dry-run only; pass --apply to delete selected hard snapshot blobs")
        return 0

    deleted = 0
    deleted_bytes = 0
    for record in selected:
        path = record["snapshot_path"]
        if path.exists():
            deleted_bytes += path.stat().st_size
            path.unlink()
            deleted += 1
        mark_snapshot_deleted(store, record)
    print(f"deleted snapshots: {deleted}")
    print(f"deleted bytes: {format_bytes(deleted_bytes)}")
    return 0


def capsule_help(args: argparse.Namespace) -> int:
    topic = args.topic or "overview"
    if args.topics:
        for name in sorted(HELP_TOPICS):
            print(name)
        return 0
    if topic not in HELP_TOPICS:
        available = ", ".join(sorted(HELP_TOPICS))
        raise RuntimeError(f"Unknown help topic: {topic}. Available: {available}")
    print(HELP_TOPICS[topic].strip())
    return 0


def require_job_param(params: JSONDict, key: str) -> Any:
    if key not in params:
        raise RuntimeError(f"Job params missing required key: {key}")
    return params[key]


def job_path(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base / path


def supported_job_types() -> set[str]:
    return {
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


def validate_job_packet(packet: JSONDict) -> None:
    required = ["schema_version", "job_id", "job_type", "created_at", "params"]
    for key in required:
        if key not in packet:
            raise RuntimeError(f"Job packet missing required key: {key}")
    if packet["schema_version"] != "0.1":
        raise RuntimeError("Job packet schema_version must be 0.1")
    if packet["job_type"] not in supported_job_types():
        raise RuntimeError(f"Unsupported job_type: {packet['job_type']}")
    if not isinstance(packet["params"], dict):
        raise RuntimeError("Job packet params must be an object")
    secret_keys = sorted(set(packet["params"]) & SECRET_JOB_PARAM_KEYS)
    if secret_keys:
        joined = ", ".join(secret_keys)
        raise RuntimeError(f"Job packet params must not contain secret input keys: {joined}. Use job runner flags.")


def print_job_plan(packet: JSONDict) -> None:
    print(f"job: {packet['job_id']}")
    print(f"type: {packet['job_type']}")
    params = packet["params"]
    for key in sorted(params):
        print(f"  {key}: {params[key]}")


def validate_capsule_job(store: Store, params: JSONDict) -> int:
    thread_id = str(require_job_param(params, "thread_id"))
    ledger = store.load_ledger(thread_id)
    capsule_id = params.get("capsule_id") or ledger.get("active_capsule_id")
    if capsule_id is None:
        raise RuntimeError(f"Thread has no active capsule: {thread_id}")
    link = find_capsule_link(ledger, str(capsule_id))
    if link is None:
        raise RuntimeError(f"Capsule not found in thread ledger: {capsule_id}")

    manifest = load_manifest_ref(store, link["manifest_ref"])
    endpoint_id = str(params.get("endpoint_id") or ledger["endpoint_id"])
    endpoint = store.load_endpoint(endpoint_id)
    assert_manifest_compatible(manifest, endpoint)

    storage = manifest.get("storage", {})
    snapshot_ref = storage.get("snapshot_ref")
    snapshot_exists = None
    if snapshot_ref:
        snapshot_exists = (store.root / snapshot_ref).exists()

    print(f"thread: {thread_id}")
    print(f"capsule: {capsule_id}")
    print(f"endpoint: {endpoint_id}")
    print("compatible: yes")
    print(f"storage mode: {storage.get('mode')}")
    if snapshot_ref:
        print(f"snapshot: {snapshot_ref}")
        print(f"snapshot exists: {'yes' if snapshot_exists else 'no'}")
    if params.get("require_snapshot") and snapshot_ref and not snapshot_exists:
        return 1
    return 0


def gateway_base_url(params: JSONDict) -> str:
    base = str(params.get("gateway_url") or "http://127.0.0.1:8765").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def gateway_timeout(params: JSONDict) -> float:
    return float(params.get("timeout", 120.0))


def read_gateway_job_auth_token(token_file: Path | None, token_env: str | None) -> str | None:
    if token_file and token_env:
        raise RuntimeError("Use only one gateway job auth token source: --gateway-auth-token-file or --gateway-auth-token-env")
    token: str | None = None
    if token_file:
        token = token_file.read_text(encoding="utf-8").strip()
    elif token_env:
        token = os.environ.get(token_env)
        if token is None:
            raise RuntimeError(f"Gateway job auth token environment variable is not set: {token_env}")
        token = token.strip()
    if token is not None and not token:
        raise RuntimeError("Gateway job auth token is empty")
    return token


def gateway_job_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    token = read_gateway_job_auth_token(
        getattr(args, "gateway_auth_token_file", None),
        getattr(args, "gateway_auth_token_env", None),
    )
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


def gateway_request_json(
    method: str,
    url: str,
    payload: JSONDict | None,
    timeout: float,
    auth_headers: dict[str, str] | None = None,
) -> JSONDict:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = dict(auth_headers or {})
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except error.HTTPError as exc:
        body = exc.read()
        raise RuntimeError(body.decode("utf-8", errors="replace")) from exc
    if not body:
        return {}
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Gateway response was not a JSON object")
    return data


def gateway_download_bundle(params: JSONDict, job_file: Path, auth_headers: dict[str, str]) -> int:
    bundle_id = str(require_job_param(params, "bundle_id"))
    out = job_path(job_file.parent, str(require_job_param(params, "out")))
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"{gateway_base_url(params)}/api/capsules/bundles/{quote(bundle_id)}"
    req = request.Request(url, headers=auth_headers, method="GET")
    try:
        with request.urlopen(req, timeout=gateway_timeout(params)) as response:
            body = response.read()
    except error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc
    out.write_bytes(body)
    print(f"downloaded bundle: {out}")
    print(f"bundle_id: {bundle_id}")
    print(f"bytes: {len(body)}")
    print(f"sha256: {digest_file(out)}")
    return 0


def gateway_import_bundle_job(params: JSONDict, job_file: Path, auth_headers: dict[str, str]) -> int:
    base = gateway_base_url(params)
    timeout = gateway_timeout(params)
    if "bundle" in params:
        source = job_path(job_file.parent, str(params["bundle"]))
        if not source.exists():
            raise FileNotFoundError(f"Bundle not found: {source}")
        headers = dict(auth_headers)
        headers["Content-Type"] = str(params.get("content_type") or "application/vnd.session-capsule.scap")
        if params.get("bundle_id"):
            headers["X-Capsule-Bundle-Id"] = str(params["bundle_id"])
        if params.get("force"):
            headers["X-Capsule-Import-Force"] = "true"
        req = request.Request(
            f"{base}/api/capsules/import",
            data=source.read_bytes(),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc
        if not isinstance(data, dict):
            raise RuntimeError("Gateway response was not a JSON object")
    else:
        payload = {
            "bundle_id": str(require_job_param(params, "bundle_id")),
            "force": bool(params.get("force", False)),
        }
        data = gateway_request_json("POST", f"{base}/api/capsules/import", payload, timeout, auth_headers)
    print(json.dumps(data, indent=2))
    return 0


def gateway_transport_job(job_type: str, params: JSONDict, job_file: Path, auth_headers: dict[str, str]) -> int:
    base = gateway_base_url(params)
    timeout = gateway_timeout(params)
    if job_type == "gateway_export_bundle":
        payload = {
            "thread_id": str(require_job_param(params, "thread_id")),
            "include_snapshots": bool(params.get("include_snapshots", False)),
            "redact_transcript": bool(params.get("redact_transcript", False)),
            "force": bool(params.get("force", False)),
        }
        if params.get("bundle_id"):
            payload["bundle_id"] = str(params["bundle_id"])
        data = gateway_request_json("POST", f"{base}/api/capsules/export", payload, timeout, auth_headers)
        print(json.dumps(data, indent=2))
        return 0
    if job_type == "gateway_list_bundles":
        data = gateway_request_json("GET", f"{base}/api/capsules/bundles", None, timeout, auth_headers)
        print(json.dumps(data, indent=2))
        return 0
    if job_type == "gateway_download_bundle":
        return gateway_download_bundle(params, job_file, auth_headers)
    if job_type == "gateway_import_bundle":
        return gateway_import_bundle_job(params, job_file, auth_headers)
    if job_type == "gateway_delete_bundle":
        bundle_id = str(require_job_param(params, "bundle_id"))
        data = gateway_request_json("DELETE", f"{base}/api/capsules/bundles/{quote(bundle_id)}", None, timeout, auth_headers)
        print(json.dumps(data, indent=2))
        return 0
    raise RuntimeError(f"Unsupported gateway transport job_type: {job_type}")


def run_job_packet(args: argparse.Namespace) -> int:
    job_file = args.job.resolve()
    packet = read_json(job_file)
    validate_job_packet(packet)
    if args.dry_run or packet.get("dry_run"):
        print_job_plan(packet)
        return 0

    params = packet["params"]
    job_type = packet["job_type"]
    store = Store(args.state_dir)

    if job_type == "resume_thread":
        resume_args = argparse.Namespace(
            state_dir=args.state_dir,
            thread=str(require_job_param(params, "thread_id")),
            slot=int(params.get("slot", 0)),
            capsule_id=params.get("capsule_id"),
            append_diff=bool(params.get("append_diff", False)),
            chat_path=str(params.get("chat_path", "/v1/chat/completions")),
            max_tokens=int(params.get("max_tokens", 0)),
            temperature=float(params.get("temperature", 0.0)),
            timeout=float(params.get("timeout", 120.0)),
        )
        return resume_thread(resume_args)

    if job_type == "checkpoint_thread":
        mode = str(params.get("mode", "soft"))
        checkpoint_args = argparse.Namespace(
            state_dir=args.state_dir,
            thread=str(require_job_param(params, "thread_id")),
            slot=int(params.get("slot", 0)),
            capsule_id=params.get("capsule_id"),
            runtime_filename=params.get("runtime_filename"),
            timeout=float(params.get("timeout", 120.0)),
        )
        if mode == "hard":
            return checkpoint_hard(checkpoint_args)
        if mode == "soft":
            return checkpoint_soft(checkpoint_args)
        raise RuntimeError("checkpoint_thread mode must be soft or hard")

    if job_type == "shutdown_thread":
        shutdown_args = argparse.Namespace(
            state_dir=args.state_dir,
            thread=str(require_job_param(params, "thread_id")),
            slot=int(params.get("slot", 0)),
            capsule_id=params.get("capsule_id"),
            runtime_filename=params.get("runtime_filename"),
            timeout=float(params.get("timeout", 120.0)),
            force=bool(params.get("force", False)),
        )
        return shutdown_thread(shutdown_args)

    if job_type == "export_thread":
        export_args = argparse.Namespace(
            state_dir=args.state_dir,
            thread=str(require_job_param(params, "thread_id")),
            out=job_path(job_file.parent, str(require_job_param(params, "out"))),
            include_snapshots=bool(params.get("include_snapshots", False)),
            redact_transcript=bool(params.get("redact_transcript", False)),
            signature_key_file=getattr(args, "signature_key_file", None),
            signature_key_env=getattr(args, "signature_key_env", None),
            signature_key_id=getattr(args, "signature_key_id", None),
            dry_run=False,
            force=bool(params.get("force", False)),
        )
        return export_bundle(export_args)

    if job_type == "validate_capsule":
        return validate_capsule_job(store, params)

    if job_type.startswith("gateway_"):
        return gateway_transport_job(job_type, params, job_file, gateway_job_auth_headers(args))

    raise RuntimeError(f"Unsupported job_type: {job_type}")


def inspect(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    if args.thread:
        ledger = store.load_ledger(args.thread)
        rows = read_jsonl(store.transcript_path(args.thread))
        print(f"thread: {ledger['thread_id']}")
        print(f"display: {ledger.get('display_name', '')}")
        print(f"endpoint: {ledger['endpoint_id']}")
        print(f"messages: {len(rows)}")
        print(f"active capsule: {ledger.get('active_capsule_id')}")
        print(f"capsules: {len(ledger.get('capsules', []))}")
        print(f"open diffs: {len(ledger.get('open_diffs', []))}")
        print(f"fallback: {ledger['fallback']['mode']} from token {ledger['fallback']['replay_start_token']}")
        return 0

    endpoints = sorted(store.endpoints_dir.glob("*.json")) if store.endpoints_dir.exists() else []
    threads = sorted(store.threads_dir.glob("*/thread-ledger.json")) if store.threads_dir.exists() else []
    print(f"state dir: {store.root}")
    print(f"endpoints: {len(endpoints)}")
    for path in endpoints:
        endpoint = read_json(path)
        print(f"  {endpoint['endpoint_id']} ({endpoint['type']}) -> {endpoint['base_url']}")
    print(f"threads: {len(threads)}")
    for path in threads:
        ledger = read_json(path)
        print(f"  {ledger['thread_id']} active={ledger.get('active_capsule_id')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Session Capsule ledgers and soft checkpoints.")
    parser.add_argument("--state-dir", type=Path, default=Path(".capsules"))
    subcommands = parser.add_subparsers(dest="command", required=True)

    endpoint = subcommands.add_parser("endpoint")
    endpoint_sub = endpoint.add_subparsers(dest="endpoint_command", required=True)

    add = endpoint_sub.add_parser("add")
    add.add_argument("endpoint_id")
    add.add_argument("--type", choices=["llamacpp", "ollama", "vllm", "openai_compatible", "hosted"], required=True)
    add.add_argument("--base-url", required=True)
    add.add_argument("--runtime-name", default="")
    add.add_argument("--runtime-build", default="unknown")
    add.add_argument("--model-ref", default="unknown")
    add.add_argument("--model-hash", default="unknown")
    add.add_argument("--tokenizer-hash", default="unknown")
    add.add_argument("--context-limit", type=int, default=131072)
    add.add_argument("--slot-field", default="id_slot")
    add.add_argument("--slot-save-restore", action="store_true")
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=endpoint_add)

    doctor = endpoint_sub.add_parser("doctor")
    doctor.add_argument("endpoint_id")
    doctor.add_argument("--timeout", type=float, default=2.0)
    doctor.add_argument("--strict", action="store_true")
    doctor.set_defaults(func=endpoint_doctor)

    thread = subcommands.add_parser("thread")
    thread_sub = thread.add_subparsers(dest="thread_command", required=True)

    start = thread_sub.add_parser("start")
    start.add_argument("--endpoint", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--thread-id")
    start.add_argument("--workspace")
    start.add_argument("--prefill", help="Named prefill capsule to attach as the thread root.")
    start.add_argument("--prefill-version", help="Specific prefill version. Defaults to active version.")
    start.add_argument("--force", action="store_true")
    start.set_defaults(func=thread_start)

    append = thread_sub.add_parser("append")
    append.add_argument("--thread", required=True)
    append.add_argument("--role", choices=["system", "user", "assistant", "tool"], default="user")
    append.add_argument("--content")
    append.add_argument("--file")
    append.set_defaults(func=thread_append)

    checkpoint = subcommands.add_parser("checkpoint")
    checkpoint.add_argument("--thread", required=True)
    checkpoint.add_argument("--soft", action="store_true", help="Create a transcript-only checkpoint.")
    checkpoint.add_argument("--hard", action="store_true", help="Save a runtime slot snapshot as a hard checkpoint.")
    checkpoint.add_argument("--slot", type=int, default=0, help="Runtime slot to save when using --hard.")
    checkpoint.add_argument("--capsule-id")
    checkpoint.add_argument("--runtime-filename", help="Server-visible filename to pass to the slot save API.")
    checkpoint.add_argument("--timeout", type=float, default=120.0)
    checkpoint.set_defaults(func=None)

    resume = subcommands.add_parser("resume")
    resume.add_argument("--thread", required=True)
    resume.add_argument("--slot", type=int, default=0)
    resume.add_argument("--capsule-id")
    resume.add_argument("--append-diff", action="store_true")
    resume.add_argument("--chat-path", default="/v1/chat/completions")
    resume.add_argument("--max-tokens", type=int, default=0)
    resume.add_argument("--temperature", type=float, default=0.0)
    resume.add_argument("--timeout", type=float, default=120.0)
    resume.set_defaults(func=resume_thread)

    shutdown = subcommands.add_parser("shutdown")
    shutdown.add_argument("--thread", required=True)
    shutdown.add_argument("--slot", type=int, default=0)
    shutdown.add_argument("--capsule-id")
    shutdown.add_argument("--runtime-filename", help="Server-visible filename to pass to the slot save API.")
    shutdown.add_argument("--timeout", type=float, default=120.0)
    shutdown.add_argument("--force", action="store_true")
    shutdown.set_defaults(func=shutdown_thread)

    export = subcommands.add_parser("export")
    export.add_argument("--thread", required=True)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--include-snapshots", action="store_true")
    export.add_argument("--redact-transcript", action="store_true")
    export.add_argument("--signature-key-file", type=Path)
    export.add_argument("--signature-key-env")
    export.add_argument("--signature-key-id")
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--force", action="store_true")
    export.set_defaults(func=export_bundle)

    import_cmd = subcommands.add_parser("import")
    import_cmd.add_argument("bundle", type=Path)
    import_cmd.add_argument("--thread-id")
    import_cmd.add_argument("--signature-key-file", type=Path)
    import_cmd.add_argument("--signature-key-env")
    import_cmd.add_argument("--require-signature", action="store_true")
    import_cmd.add_argument("--force", action="store_true")
    import_cmd.set_defaults(func=import_bundle)

    verify_cmd = subcommands.add_parser("verify")
    verify_cmd.add_argument("bundle", type=Path)
    verify_cmd.add_argument("--signature-key-file", type=Path)
    verify_cmd.add_argument("--signature-key-env")
    verify_cmd.add_argument("--require-signature", action="store_true")
    verify_cmd.set_defaults(func=verify_bundle)

    job = subcommands.add_parser("job")
    job_sub = job.add_subparsers(dest="job_command", required=True)

    job_run = job_sub.add_parser("run")
    job_run.add_argument("job", type=Path)
    job_run.add_argument("--dry-run", action="store_true")
    job_run.add_argument("--signature-key-file", type=Path)
    job_run.add_argument("--signature-key-env")
    job_run.add_argument("--signature-key-id")
    job_run.add_argument("--gateway-auth-token-file", type=Path)
    job_run.add_argument("--gateway-auth-token-env")
    job_run.set_defaults(func=run_job_packet)

    config_cmd = subcommands.add_parser("config")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)

    config_init_parser = config_sub.add_parser("init")
    config_init_parser.add_argument("--force", action="store_true")
    config_init_parser.set_defaults(func=config_init)

    config_show_parser = config_sub.add_parser("show")
    config_show_parser.add_argument("key", nargs="?")
    config_show_parser.set_defaults(func=config_show)

    config_set_parser = config_sub.add_parser("set")
    config_set_parser.add_argument("key")
    config_set_parser.add_argument("value")
    config_set_parser.set_defaults(func=config_set)

    stats_cmd = subcommands.add_parser("stats")
    stats_cmd.set_defaults(func=storage_stats)

    pin_cmd = subcommands.add_parser("pin")
    pin_cmd.add_argument("--thread", required=True)
    pin_cmd.add_argument("--capsule-id", help="Defaults to the active capsule for the thread.")
    pin_cmd.set_defaults(func=pin_capsule)

    unpin_cmd = subcommands.add_parser("unpin")
    unpin_cmd.add_argument("--thread", required=True)
    unpin_cmd.add_argument("--capsule-id", help="Defaults to the active capsule for the thread.")
    unpin_cmd.set_defaults(func=unpin_capsule)

    gc_cmd = subcommands.add_parser("gc")
    gc_cmd.add_argument("--dry-run", action="store_true", help="Show deletion plan without deleting. This is the default.")
    gc_cmd.add_argument("--apply", action="store_true", help="Delete selected unpinned hard snapshot blobs.")
    gc_cmd.add_argument("--max-bytes", help="One-run override for storage.max_bytes, e.g. 50GB.")
    gc_cmd.add_argument("--min-free-bytes", help="One-run override for storage.min_free_bytes, e.g. 20GB.")
    gc_cmd.set_defaults(func=gc_storage)

    help_cmd = subcommands.add_parser("help", help="Show Session Capsules conceptual help.")
    help_cmd.add_argument("topic", nargs="?", help="Help topic. Defaults to overview.")
    help_cmd.add_argument("--topics", action="store_true", help="List available help topics.")
    help_cmd.set_defaults(func=capsule_help)

    prefill = subcommands.add_parser("prefill")
    prefill_sub = prefill.add_subparsers(dest="prefill_command", required=True)

    prefill_create_parser = prefill_sub.add_parser("create")
    prefill_create_parser.add_argument("--endpoint", required=True)
    prefill_create_parser.add_argument("--name", required=True)
    prefill_create_parser.add_argument("--kind", choices=["user_prefill", "project_prefill"], default="user_prefill")
    prefill_create_parser.add_argument("--input")
    prefill_create_parser.add_argument("--content")
    prefill_create_parser.add_argument("--version")
    prefill_create_parser.add_argument("--role", default="system")
    prefill_create_parser.add_argument("--soft", action="store_true", help="Create a source-only prefill manifest.")
    prefill_create_parser.add_argument("--hard", action="store_true", help="Compile source into a runtime slot and save it.")
    prefill_create_parser.add_argument("--slot", type=int, default=0)
    prefill_create_parser.add_argument("--runtime-filename", help="Server-visible filename to pass to the slot save API.")
    prefill_create_parser.add_argument("--chat-path", default="/v1/chat/completions")
    prefill_create_parser.add_argument("--temperature", type=float, default=0.0)
    prefill_create_parser.add_argument("--timeout", type=float, default=120.0)
    prefill_create_parser.add_argument("--force", action="store_true")
    prefill_create_parser.set_defaults(func=prefill_create)

    prefill_list_parser = prefill_sub.add_parser("list")
    prefill_list_parser.add_argument("--verbose", action="store_true")
    prefill_list_parser.set_defaults(func=prefill_list)

    prefill_diff_parser = prefill_sub.add_parser("diff")
    prefill_diff_parser.add_argument("--name", required=True)
    prefill_diff_parser.add_argument("--version")
    prefill_diff_parser.add_argument("--input")
    prefill_diff_parser.add_argument("--content")
    prefill_diff_parser.add_argument("--strict", action="store_true")
    prefill_diff_parser.set_defaults(func=prefill_diff)

    inspect_cmd = subcommands.add_parser("inspect")
    inspect_cmd.add_argument("--thread")
    inspect_cmd.set_defaults(func=inspect)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    try:
        if getattr(args, "command", "") == "prefill" and getattr(args, "prefill_command", "") == "create":
            if args.soft and args.hard:
                print("Choose at most one prefill mode: --soft or --hard.", file=sys.stderr)
                return 2
        if getattr(args, "command", "") == "checkpoint":
            if args.soft == args.hard:
                print("Choose exactly one checkpoint mode: --soft or --hard.", file=sys.stderr)
                return 2
            if args.hard:
                return checkpoint_hard(args)
            return checkpoint_soft(args)
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - CLI should produce concise errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
