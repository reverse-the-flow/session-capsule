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
import subprocess
import sys
import tempfile
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
  security   bundle integrity, signing, and local sealing

Start here:
  py -3 .\\scripts\\capsule_cli.py config init
  py -3 .\\scripts\\capsule_cli.py endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080
  py -3 .\\scripts\\capsule_cli.py thread start --endpoint local-llamacpp --name research-loop
  py -3 .\\scripts\\capsule_cli.py inspect

More:
  capsule help config
  capsule help gateway
  capsule help integrations
  capsule help transport
  capsule help security
  capsule help sealing
  capsule help roadmap
  capsule help storage
  capsule help state
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
  --state-dir, --host, --port, --endpoint, --slot, --checkpoint-mode, --timeout, --cors-allow-origin""",
    "state": """Capsule state is project-local by default.

Default state directory:
  .capsules/

Inspect state location:
  py -3 .\\scripts\\capsule_cli.py state info

Override state for tests, shared workspaces, or Model Plane launch profiles:
  py -3 .\\scripts\\capsule_cli.py --state-dir C:\\path\\to\\.capsules state info

V0 policy:
  .capsules/ is the default and recommended project-local state root
  --state-dir is the explicit override
  user-level/global state is a future integration option, not the default
  manifests store paths relative to the selected state root""",
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
  py -3 .\\scripts\\capsule_cli.py endpoint doctor local-llamacpp --strict

Doctor also tries a non-fatal runtime metadata probe:
  --runtime-metadata-path /props
  --skip-runtime-metadata

Summarize endpoint slot compatibility:
  py -3 .\\scripts\\capsule_cli.py endpoint matrix
  py -3 .\\scripts\\capsule_cli.py endpoint matrix --json

Doctor records slot probe evidence in the endpoint record:
  /slots response shape
  sample slot keys
  candidate slot identity fields
  configured chat slot field
  visible n_ctx and is_processing fields
  runtime build/model/context fields when the metadata endpoint exposes them""",
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

Client-native thread headers recognized:
  X-OpenWebUI-Chat-Id
  X-Opencode-Thread
  X-Opencode-Session

Discover the full identity contract:
  GET /api/capsules/status

For gateway upload/download endpoints:
  capsule help transport""",
    "integrations": """Integrations should point existing clients at the local gateway and pass stable identity headers.

Preferred gateway target:
  http://127.0.0.1:8765/v1

Preferred identity headers:
  X-Capsule-Thread
  X-Capsule-Workspace
  X-Capsule-Prefill

Generate an opencode provider config with concrete capsule headers:
  py -3 .\\scripts\\capsule_cli.py integration opencode-config --workspace . --session default --prefill user_default --out .\\.capsules\\integrations\\opencode.generated.json

The generated opencode config keeps the gateway token as an environment reference:
  {env:CAPSULE_GATEWAY_TOKEN}

Native opencode hook boundary:
  generated provider configs remain the supported path until opencode exposes a provider-request/header hook or session-aware provider header template

Open WebUI can use the gateway as an OpenAI-compatible API base URL and should forward chat/user headers when available.

More:
  docs/integrations.md""",
    "transport": """Gateway transport lets a local UI or Model Plane move .scap bundles without reimplementing export/import.

Endpoints:
  GET    /api/capsules/status
  POST   /api/capsules/export
  GET    /api/capsules/bundles
  POST   /api/capsules/bundles
  GET    /api/capsules/bundles/{bundle_id}
  POST   /api/capsules/import
  DELETE /api/capsules/bundles/{bundle_id}

Model Plane should read /api/capsules/status first. The response includes a versioned transport object with endpoint paths, max_upload_bytes, content type, auth policy, signing policy, and advertised upload/download capabilities. Launch profiles can list transport.required_capabilities; gateway check verifies every listed capability before Model Plane enables profile-dependent controls.
Browser-hosted Model Plane UIs should launch the gateway with --cors-allow-origin set to the exact UI origin, then require transport.cors.enabled before enabling direct browser upload/download controls.

Bundles are stored under:
  .capsules/bundles/

Export is ledger-only by default. Hard snapshots require include_snapshots=true.

Raw upload content type:
  application/vnd.session-capsule.scap

Upload size limit:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --max-bundle-bytes 5GB

Browser preflight:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --cors-allow-origin http://127.0.0.1:3000

Raw upload import target thread header:
  X-Capsule-Import-Thread

Store-only upload:
  POST /api/capsules/bundles stores a verified .scap without creating thread state

Gateway-side import policy:
  py -3 .\\scripts\\capsule_gateway.py --state-dir .\\.capsules --endpoint local-llamacpp --bundle-policy-preset metadata-only

Direct gateway client commands:
  py -3 .\\scripts\\capsule_cli.py gateway status --url http://127.0.0.1:8765 --json
  py -3 .\\scripts\\capsule_cli.py gateway export --url http://127.0.0.1:8765 --thread research-loop --bundle-id research-loop
  py -3 .\\scripts\\capsule_cli.py gateway list --url http://127.0.0.1:8765
  py -3 .\\scripts\\capsule_cli.py gateway download --url http://127.0.0.1:8765 --bundle-id research-loop --out .\\research-loop.scap
  py -3 .\\scripts\\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\\research-loop.scap --bundle-id stored-research-loop
  py -3 .\\scripts\\capsule_cli.py gateway upload --url http://127.0.0.1:8765 --bundle .\\research-loop.scap --bundle-id uploaded-research-loop --thread-id research-loop-copy
  py -3 .\\scripts\\capsule_cli.py gateway delete --url http://127.0.0.1:8765 --bundle-id uploaded-research-loop""",
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

Metadata-only redacted export:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop-redacted.scap --redact-transcript

Import:
  py -3 .\\scripts\\capsule_cli.py import .\\research-loop.scap

Import as a new local thread id:
  py -3 .\\scripts\\capsule_cli.py import .\\research-loop.scap --thread-id research-loop-copy

Import warns when an incoming endpoint id already exists locally with different runtime metadata.

Verify bundle integrity:
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap

Inspect bundle share/import posture:
  py -3 .\\scripts\\capsule_cli.py inspect --bundle .\\research-loop.scap
  py -3 .\\scripts\\capsule_cli.py inspect --bundle .\\research-loop.scap --json

Fail unless a bundle is metadata-only before sharing:
  py -3 .\\scripts\\capsule_cli.py bundle-policy .\\research-loop.scap --preset metadata-only

Sign with an explicit local key file:
  py -3 .\\scripts\\capsule_cli.py export --thread research-loop --out .\\research-loop.scap --signature-key-file .\\capsule-signing.key --signature-key-id local

If snapshots are omitted, transcript replay remains the fallback unless the bundle was exported with --redact-transcript.
Redaction omits transcript and prefill source text, but it is not encryption.

For gateway upload/download transport:
  capsule help transport""",
    "security": """Security status:
  implemented: per-entry sha256 file_digests in exported .scap bundles
  implemented: optional HMAC-SHA256 bundle signatures
  implemented: external age-compatible sealed bundle envelopes
  implemented: metadata-only redacted transcript export
  implemented: capsule verify rejects duplicate or digest-mismatched bundle entries
  implemented: import verifies bundles that include file_digests
  implemented: import warns on local endpoint metadata conflicts
  not implemented yet: hosted provider-side sealed capsules

Commands:
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap
  py -3 .\\scripts\\capsule_cli.py inspect --bundle .\\research-loop.scap --json
  py -3 .\\scripts\\capsule_cli.py bundle-policy .\\research-loop.scap --preset signed-metadata-only
  py -3 .\\scripts\\capsule_cli.py verify .\\research-loop.scap --signature-key-file .\\capsule-signing.key --require-signature
  py -3 .\\scripts\\capsule_cli.py seal .\\research-loop.scap --out .\\research-loop.sealed.scap --age-recipient age1...
  py -3 .\\scripts\\capsule_cli.py unseal .\\research-loop.sealed.scap --out .\\research-loop.unsealed.scap --age-identity .\\age-identity.txt

Key handling:
  --signature-key-file reads a local key file for this command only
  --signature-key-env reads a key from an environment variable
  keys are not written into .capsules state

HMAC signing proves possession of the shared key. Sealing delegates encryption to an external age-compatible command instead of implementing local crypto.

More:
  capsule help sealing""",
    "sealing": """Sealed .scap envelopes use an external age-compatible command.

Recommended backend:
  age CLI or an age-compatible executable on PATH

Recommended key handling:
  recipient files may live with project launch policy because they contain public key material
  identity files are private keys and should live outside .capsules, ideally in an operator secret path or OS-managed secret store
  job packets and gateway launch profiles should carry references only, not key values
  sealed .scap bundles must be explicitly unsealed before import

Seal with an inline public recipient:
  py -3 .\\scripts\\capsule_cli.py seal .\\research-loop.scap --out .\\research-loop.sealed.scap --age-recipient age1...

Seal with a recipient file:
  py -3 .\\scripts\\capsule_cli.py seal .\\research-loop.scap --out .\\research-loop.sealed.scap --age-recipient-file .\\.capsules\\security\\recipients\\local.agepub

Check before sharing or storing:
  py -3 .\\scripts\\capsule_cli.py bundle-policy .\\research-loop.sealed.scap --preset sealed
  py -3 .\\scripts\\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\\research-loop.sealed.scap --policy-preset sealed

Unseal with an operator-private identity file:
  py -3 .\\scripts\\capsule_cli.py unseal .\\research-loop.sealed.scap --out .\\research-loop.unsealed.scap --age-identity C:\\Users\\you\\.config\\age\\keys.txt

Boundary:
  local sealed envelopes are implemented
  hosted/provider-side sealed capsules are future work
  this repo delegates crypto to age instead of implementing cryptographic primitives""",
    "roadmap": """Roadmap and readiness files:
  docs/roadmap.md
  docs/v0-readiness.md

Standalone v0 readiness gate:
  py -3 .\\scripts\\run_smoke_tests.py

Current status:
  no tracked open questions for standalone v0
  local harness and gateway scope is implementation-complete
  future provider/runtime work remains explicitly out of v0

Deliberate non-goals:
  hosted/provider-side sealed capsules
  user-carried runtime snapshots portable across model backends
  model weights inside capsule bundles
  passive browser/app watchers
  native opencode replacement before a provider-request/header hook exists
  Model Plane owning model weights, live KV tensors, runtime slot layout, or inference loop""",
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

Direct gateway transport commands:
  py -3 .\\scripts\\capsule_cli.py gateway status --url http://127.0.0.1:8765 --auth-token-file .\\capsule-gateway-token --json
  py -3 .\\scripts\\capsule_cli.py gateway download --url http://127.0.0.1:8765 --bundle-id research-loop --out .\\research-loop.scap --auth-token-file .\\capsule-gateway-token
  py -3 .\\scripts\\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\\research-loop.scap --bundle-id stored-research-loop --auth-token-file .\\capsule-gateway-token
  py -3 .\\scripts\\capsule_cli.py gateway upload --url http://127.0.0.1:8765 --bundle .\\research-loop.scap --bundle-id uploaded-research-loop --thread-id research-loop-copy --auth-token-file .\\capsule-gateway-token

Gate local uploads before sending bytes:
  py -3 .\\scripts\\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\\research-loop.sealed.scap --policy-preset sealed --auth-token-file .\\capsule-gateway-token
  py -3 .\\scripts\\capsule_cli.py gateway upload --url http://127.0.0.1:8765 --bundle .\\research-loop-redacted.scap --policy-preset metadata-only --auth-token-file .\\capsule-gateway-token

Gateway-side import policy can also be set in the launch profile:
  security.bundle_import_policy

Public sealing policy for user-carried transfer can also be set in the launch profile:
  security.bundle_sealing

Gateway launch profile:
  schemas/model-plane-gateway-launch.schema.json
  examples/model-plane/gateway-launch-profile.example.json

Render a launch command:
  py -3 .\\scripts\\capsule_cli.py gateway command .\\examples\\model-plane\\gateway-launch-profile.example.json --json

Check a launched gateway:
  py -3 .\\scripts\\capsule_cli.py gateway check .\\examples\\model-plane\\gateway-launch-profile.example.json --json

Gateway health endpoint for launch profiles:
  /api/capsules/status

Gateway command reports bundle_sealing.seal_command_template when the profile includes a public age recipient file. Gateway check reports transport_verified, endpoint_verified, endpoint_compatibility, required_capabilities, and transport_capabilities. Hard checkpoint profiles require a slot_probe_ok endpoint.

Gateway import jobs may use params.thread_id as the target local thread id for the imported bundle.

Supported job packet types:
  resume_thread
  checkpoint_thread
  shutdown_thread
  export_thread
  validate_capsule
  gateway_export_bundle
  gateway_list_bundles
  gateway_store_bundle
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


def endpoint_url(base_url: str, path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return base_url.rstrip("/") + normalized


def scalar_metadata_value(payload: Any, candidate_keys: set[str], max_depth: int = 4) -> Any:
    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in candidate_keys and isinstance(item, (str, int, float, bool)):
                    return item
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
        elif isinstance(value, list):
            for item in value[:8]:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
    return None


def int_metadata_value(payload: Any, candidate_keys: set[str]) -> int | None:
    value = scalar_metadata_value(payload, candidate_keys)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def string_metadata_value(payload: Any, candidate_keys: set[str]) -> str | None:
    value = scalar_metadata_value(payload, candidate_keys)
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def runtime_probe_report(payload: Any, metadata_url: str, elapsed_ms: float) -> JSONDict:
    if isinstance(payload, dict):
        response_shape = "object"
        sample_keys = sorted(str(key) for key in payload)[:32]
    elif isinstance(payload, list):
        response_shape = "list"
        sample_keys = []
        for item in payload[:8]:
            if isinstance(item, dict):
                sample_keys.extend(str(key) for key in item)
        sample_keys = sorted(set(sample_keys))[:32]
    else:
        response_shape = type(payload).__name__
        sample_keys = []

    build = string_metadata_value(payload, {"build", "build_id", "build_info", "commit", "git_commit", "version"})
    model_ref = string_metadata_value(payload, {"model", "model_ref", "model_name", "model_path", "model_alias"})
    model_hash = string_metadata_value(payload, {"model_hash", "model_sha256", "model_digest"})
    tokenizer_hash = string_metadata_value(payload, {"tokenizer_hash", "tokenizer_sha256", "tokenizer_digest"})
    context_limit = int_metadata_value(payload, {"context_limit", "ctx_size", "n_ctx", "n_ctx_train"})
    observed_fields = sorted(
        key
        for key, value in {
            "build": build,
            "model_ref": model_ref,
            "model_hash": model_hash,
            "tokenizer_hash": tokenizer_hash,
            "context_limit": context_limit,
        }.items()
        if value is not None
    )
    return {
        "status": "runtime_probe_ok",
        "metadata_url": metadata_url,
        "client_duration_ms": elapsed_ms,
        "response_shape": response_shape,
        "sample_keys": sample_keys,
        "observed_fields": observed_fields,
        "build": build,
        "model_ref": model_ref,
        "model_hash": model_hash,
        "tokenizer_hash": tokenizer_hash,
        "context_limit": context_limit,
    }


def apply_runtime_probe(endpoint: JSONDict, probe: JSONDict) -> list[str]:
    runtime = endpoint.setdefault("runtime", {})
    updates: list[str] = []
    for key in ["build", "model_ref", "model_hash", "tokenizer_hash", "context_limit"]:
        value = probe.get(key)
        if value is not None and runtime.get(key) != value:
            runtime[key] = value
            updates.append(key)
    return updates


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


def normalize_slots_response(slots_payload: Any) -> tuple[list[JSONDict] | None, str]:
    if isinstance(slots_payload, list):
        return [item for item in slots_payload if isinstance(item, dict)], "list"
    if isinstance(slots_payload, dict):
        for key in ["slots", "data"]:
            value = slots_payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], f"object.{key}"
        return None, "object"
    return None, type(slots_payload).__name__


def slot_probe_report(slots_payload: Any, configured_slot_field: str) -> JSONDict:
    slots, response_shape = normalize_slots_response(slots_payload)
    sample_keys: set[str] = set()
    n_ctx_values: set[int] = set()
    processing_values: set[bool] = set()
    if slots is not None:
        for slot in slots[:8]:
            sample_keys.update(str(key) for key in slot)
            n_ctx = slot.get("n_ctx")
            if isinstance(n_ctx, int):
                n_ctx_values.add(n_ctx)
            is_processing = slot.get("is_processing")
            if isinstance(is_processing, bool):
                processing_values.add(is_processing)
    identity_candidates = ["id", "id_slot", "slot_id", "slot", "index"]
    identity_fields = [field for field in identity_candidates if field in sample_keys]
    return {
        "response_shape": response_shape,
        "slot_count": len(slots) if slots is not None else None,
        "sample_keys": sorted(sample_keys),
        "slot_identity_fields": identity_fields,
        "configured_slot_field": configured_slot_field,
        "configured_slot_field_seen_in_slots": configured_slot_field in sample_keys,
        "has_n_ctx": bool(n_ctx_values),
        "n_ctx_values": sorted(n_ctx_values),
        "has_is_processing": bool(processing_values),
        "is_processing_values": sorted(processing_values),
    }


def endpoint_doctor(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    endpoint = store.load_endpoint(args.endpoint_id)
    slot_api = endpoint.get("slot_api", {})
    slots_path = slot_api.get("slots_path", "/slots")
    configured_slot_field = str(slot_api.get("slot_field", "id_slot"))
    url = endpoint_url(endpoint["base_url"], slots_path)
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

    slot_probe = slot_probe_report(slots, configured_slot_field)
    slot_count = slot_probe.get("slot_count")
    slot_save_restore = isinstance(slot_count, int) and slot_count > 0
    endpoint["capabilities"]["slot_save_restore"] = slot_save_restore
    runtime_probe: JSONDict | None = None
    runtime_updates: list[str] = []
    if not args.skip_runtime_metadata:
        metadata_url = endpoint_url(endpoint["base_url"], args.runtime_metadata_path)
        try:
            metadata, metadata_elapsed_ms = get_json(metadata_url, args.timeout)
            runtime_probe = runtime_probe_report(metadata, metadata_url, metadata_elapsed_ms)
            runtime_updates = apply_runtime_probe(endpoint, runtime_probe)
        except Exception as exc:  # noqa: BLE001 - runtime metadata is helpful but non-fatal.
            runtime_probe = {
                "status": "runtime_probe_unavailable",
                "metadata_url": metadata_url,
                "error": str(exc),
            }
    endpoint["doctor"] = {
        "slots_url": url,
        "client_duration_ms": elapsed_ms,
        "slot_count": slot_count,
        "slot_probe": slot_probe,
    }
    if runtime_probe is not None:
        endpoint["doctor"]["runtime_probe"] = runtime_probe
    write_json(store.endpoint_path(args.endpoint_id), endpoint)
    print(f"endpoint reachable: yes ({elapsed_ms} ms)")
    if runtime_probe is not None:
        print(f"runtime metadata: {runtime_probe['status']}")
        if runtime_updates:
            print(f"runtime fields updated: {', '.join(runtime_updates)}")
    if slot_count is not None:
        print(f"slots: {slot_count}")
        print(f"slots response shape: {slot_probe['response_shape']}")
        identity_fields = slot_probe.get("slot_identity_fields", [])
        print(f"slot identity fields: {', '.join(identity_fields) if identity_fields else 'none'}")
        print(f"configured chat slot field: {configured_slot_field}")
        print(f"configured field seen in /slots: {'yes' if slot_probe['configured_slot_field_seen_in_slots'] else 'no'}")
    else:
        print(f"slots response was not recognized as a slot list ({slot_probe['response_shape']})")
    return 0 if slot_save_restore or not args.strict else 1


def slot_probe_status(endpoint: JSONDict) -> str:
    doctor = endpoint.get("doctor")
    if not isinstance(doctor, dict):
        return "slot_probe_missing"
    probe = doctor.get("slot_probe")
    if not isinstance(probe, dict):
        return "slot_probe_missing"
    slot_count = probe.get("slot_count")
    if not isinstance(slot_count, int):
        return "slot_probe_unrecognized"
    if slot_count <= 0:
        return "no_slots"
    capabilities = endpoint.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("slot_save_restore") is True:
        return "slot_probe_ok"
    return "slot_probe_degraded"


def endpoint_matrix_report(store: Store) -> JSONDict:
    endpoints: list[JSONDict] = []
    endpoint_paths = sorted(store.endpoints_dir.glob("*.json")) if store.endpoints_dir.exists() else []
    for path in endpoint_paths:
        endpoint = read_json(path)
        runtime = endpoint.get("runtime", {})
        capabilities = endpoint.get("capabilities", {})
        slot_api = endpoint.get("slot_api", {})
        doctor = endpoint.get("doctor", {})
        probe = doctor.get("slot_probe", {}) if isinstance(doctor, dict) else {}
        runtime_probe = doctor.get("runtime_probe", {}) if isinstance(doctor, dict) else {}
        if not isinstance(runtime, dict):
            runtime = {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        if not isinstance(slot_api, dict):
            slot_api = {}
        if not isinstance(probe, dict):
            probe = {}
        if not isinstance(runtime_probe, dict):
            runtime_probe = {}
        slot_probe = {
            "status": slot_probe_status(endpoint),
            "response_shape": probe.get("response_shape"),
            "slot_count": probe.get("slot_count"),
            "sample_keys": probe.get("sample_keys", []),
            "slot_identity_fields": probe.get("slot_identity_fields", []),
            "configured_slot_field": probe.get("configured_slot_field"),
            "configured_slot_field_seen_in_slots": probe.get("configured_slot_field_seen_in_slots"),
            "has_n_ctx": probe.get("has_n_ctx"),
            "n_ctx_values": probe.get("n_ctx_values", []),
            "has_is_processing": probe.get("has_is_processing"),
            "is_processing_values": probe.get("is_processing_values", []),
        }
        endpoints.append(
            {
                "endpoint_id": endpoint.get("endpoint_id") or path.stem,
                "type": endpoint.get("type"),
                "base_url": endpoint.get("base_url"),
                "checked_at": endpoint.get("checked_at"),
                "runtime": {
                    "name": runtime.get("name"),
                    "build": runtime.get("build"),
                    "model_ref": runtime.get("model_ref"),
                    "model_hash": runtime.get("model_hash"),
                    "tokenizer_hash": runtime.get("tokenizer_hash"),
                    "context_limit": runtime.get("context_limit"),
                },
                "capabilities": {
                    "soft_capsules": capabilities.get("soft_capsules"),
                    "slot_save_restore": capabilities.get("slot_save_restore"),
                    "server_side_handles": capabilities.get("server_side_handles"),
                    "user_carried_blobs": capabilities.get("user_carried_blobs"),
                    "sealed_blobs": capabilities.get("sealed_blobs"),
                    "transcript_replay_fallback": capabilities.get("transcript_replay_fallback"),
                },
                "slot_api": {
                    "slots_path": slot_api.get("slots_path"),
                    "save_action": slot_api.get("save_action"),
                    "restore_action": slot_api.get("restore_action"),
                    "slot_field": slot_api.get("slot_field"),
                },
                "doctor": {
                    "slots_url": doctor.get("slots_url") if isinstance(doctor, dict) else None,
                    "client_duration_ms": doctor.get("client_duration_ms") if isinstance(doctor, dict) else None,
                    "runtime_probe": {
                        "status": runtime_probe.get("status"),
                        "metadata_url": runtime_probe.get("metadata_url"),
                        "response_shape": runtime_probe.get("response_shape"),
                        "observed_fields": runtime_probe.get("observed_fields", []),
                    },
                },
                "slot_probe": slot_probe,
            }
        )
    return {
        "schema_version": "0.1",
        "report_type": "session_capsule_endpoint_matrix",
        "generated_at": now_iso(),
        "state_dir": str(store.root.resolve()),
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
    }


def format_matrix_values(values: Any) -> str:
    if isinstance(values, list):
        return ",".join(str(value) for value in values) if values else "none"
    if values is None:
        return "unknown"
    return str(values)


def endpoint_matrix(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    report = endpoint_matrix_report(store)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"state_dir: {report['state_dir']}")
    print(f"endpoints: {report['endpoint_count']}")
    for endpoint in report["endpoints"]:
        runtime = endpoint["runtime"]
        probe = endpoint["slot_probe"]
        print(
            " | ".join(
                [
                    str(endpoint["endpoint_id"]),
                    f"type={endpoint.get('type')}",
                    f"model={runtime.get('model_ref')}",
                    f"build={runtime.get('build')}",
                    f"runtime_probe={endpoint.get('doctor', {}).get('runtime_probe', {}).get('status')}",
                    f"status={probe.get('status')}",
                    f"slots={probe.get('slot_count')}",
                    f"shape={probe.get('response_shape')}",
                    f"ids={format_matrix_values(probe.get('slot_identity_fields'))}",
                    f"slot_field={probe.get('configured_slot_field')}",
                    f"n_ctx={format_matrix_values(probe.get('n_ctx_values'))}",
                ]
            )
        )
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


def export_ledger_payload(ledger: JSONDict, redact_transcript: bool) -> JSONDict:
    payload = json.loads(json.dumps(ledger))
    if not redact_transcript:
        return payload

    payload["transcript_redacted"] = True
    payload["open_diffs"] = []
    payload["fallback"] = {
        "mode": "unavailable_redacted_transcript",
        "replay_start_token": 0,
        "reason": "Transcript content was redacted from this bundle; transcript replay fallback is unavailable.",
    }
    notes = payload.setdefault("notes", [])
    if isinstance(notes, list):
        notes.append("Transcript content was redacted during bundle export.")
    return payload


def export_manifest_payload(manifest: JSONDict, redact_transcript: bool) -> JSONDict:
    payload = json.loads(json.dumps(manifest))
    if not redact_transcript:
        return payload

    prefill_source = payload.get("prefill_source")
    if isinstance(prefill_source, dict):
        prefill_source["source_ref"] = None
        prefill_source["source_redacted"] = True
        notes = payload.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append("Prefill source content was redacted during bundle export.")
    return payload


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
    ledger_payload = export_ledger_payload(ledger, redact_transcript)

    add_export_data(entries, "thread-ledger.json", pretty_json_bytes(ledger_payload))
    add_export_data(entries, "transcript.jsonl", text_bytes(transcript_content))

    state_ledger_ref = f"threads/{thread_id}/thread-ledger.json"
    add_export_data(entries, state_ledger_ref, pretty_json_bytes(ledger_payload))
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
        add_export_data(entries, manifest_ref, pretty_json_bytes(export_manifest_payload(manifest, redact_transcript)))
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


def bundle_entry_sizes(bundle: zipfile.ZipFile) -> tuple[dict[str, int], list[str], int]:
    sizes: dict[str, int] = {}
    duplicates: list[str] = []
    total_uncompressed = 0
    for item in bundle.infolist():
        if item.is_dir():
            continue
        name = safe_zip_name(item.filename)
        total_uncompressed += item.file_size
        if name in sizes:
            duplicates.append(name)
        sizes[name] = sizes.get(name, 0) + item.file_size
    return sizes, sorted(set(duplicates)), total_uncompressed


def bundle_content_classification(
    encrypted: bool,
    transcript_bytes: int,
    prefill_source_bytes: int,
    snapshots_included: bool,
) -> str:
    if encrypted:
        return "encrypted"
    if transcript_bytes or prefill_source_bytes:
        return "contains_plaintext_content"
    if snapshots_included:
        return "contains_unencrypted_snapshots"
    return "metadata_only_not_encrypted"


def bundle_share_policy(
    classification: str,
    signature_present: bool,
    encrypted: bool,
    has_plaintext_content: bool,
    snapshots_included: bool,
    redacted_transcript: bool,
) -> JSONDict:
    warnings: list[str] = []
    recommendations: list[str] = []
    if not encrypted:
        warnings.append("Bundle is not encrypted; use trusted storage and transport.")
        recommendations.append("Treat redaction as metadata reduction, not cryptographic sealing.")
    if has_plaintext_content:
        warnings.append("Transcript or prefill source text is present in plaintext.")
        recommendations.append("Use export --redact-transcript before sharing outside a trusted boundary.")
    if snapshots_included and not encrypted:
        warnings.append("Hard snapshot blobs are present without encryption.")
        recommendations.append("Omit snapshots unless the recipient has the same trusted runtime context.")
    if redacted_transcript and not encrypted:
        warnings.append("Redacted transcript bundles still expose metadata and are not sealed.")
    if not signature_present:
        warnings.append("Bundle is unsigned; authenticity is not cryptographically checked.")
        recommendations.append("Use --signature-key-file for shared-key authenticity when transporting bundles.")
    if encrypted:
        recommendations.append("Verify the sealing key policy before importing.")
    return {
        "classification": classification,
        "trusted_transport_required": not encrypted,
        "warnings": warnings,
        "recommendations": recommendations,
    }


def inspect_bundle_report(bundle_path: Path) -> JSONDict:
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_path}")
    with zipfile.ZipFile(bundle_path, "r") as bundle:
        sizes, duplicates, total_uncompressed = bundle_entry_sizes(bundle)
        if "manifest.json" not in sizes:
            raise RuntimeError("Bundle manifest is missing")
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("Bundle manifest must be a JSON object")

    integrity = manifest.get("integrity", {})
    if not isinstance(integrity, dict):
        integrity = {}
    signature = integrity.get("signature")
    signature_present = isinstance(signature, dict)
    encryption = integrity.get("encryption")
    encrypted = isinstance(encryption, dict)

    transcript_entries = sorted(name for name in sizes if name == "transcript.jsonl" or name.endswith("/transcript.jsonl"))
    prefill_source_entries = sorted(
        name for name in sizes if name.startswith("prefills/") and name.endswith("/source.md")
    )
    snapshot_entries = sorted(name for name in sizes if "/snapshots/" in name)
    transcript_bytes = sum(sizes[name] for name in transcript_entries)
    prefill_source_bytes = sum(sizes[name] for name in prefill_source_entries)
    snapshot_bytes = sum(sizes[name] for name in snapshot_entries)
    snapshots_included = bool(manifest.get("includes_snapshots")) or bool(snapshot_entries)
    classification = bundle_content_classification(
        encrypted,
        transcript_bytes,
        prefill_source_bytes,
        snapshots_included,
    )
    redacted_transcript = bool(manifest.get("redacted_transcript"))
    return {
        "bundle": str(bundle_path),
        "size_bytes": bundle_path.stat().st_size,
        "sha256": digest_file(bundle_path),
        "schema_version": manifest.get("schema_version"),
        "bundle_type": manifest.get("bundle_type"),
        "created_at": manifest.get("created_at"),
        "thread_id": manifest.get("thread_id"),
        "export_mode": manifest.get("export_mode"),
        "redacted_transcript": redacted_transcript,
        "redaction": manifest.get("redaction", {}),
        "entries": {
            "count": len(sizes),
            "total_uncompressed_bytes": total_uncompressed,
            "duplicate_entries": duplicates,
        },
        "content": {
            "transcript_entries": transcript_entries,
            "transcript_bytes": transcript_bytes,
            "transcript_included": transcript_bytes > 0,
            "prefill_source_entries": prefill_source_entries,
            "prefill_source_bytes": prefill_source_bytes,
            "prefill_sources_included": prefill_source_bytes > 0,
            "snapshot_entries": snapshot_entries,
            "snapshot_bytes": snapshot_bytes,
            "snapshots_included": snapshots_included,
            "omitted_snapshots": manifest.get("omitted_snapshots", []),
        },
        "integrity": {
            "file_digest_index_present": isinstance(manifest.get("file_digests"), dict),
            "file_digest_algorithm": integrity.get("file_digest_algorithm"),
            "signature_present": signature_present,
            "signature_algorithm": signature.get("algorithm") if signature_present else None,
            "signature_key_id": signature.get("key_id") if signature_present else None,
            "encrypted": encrypted,
            "encryption": encryption,
        },
        "share_policy": bundle_share_policy(
            classification,
            signature_present,
            encrypted,
            transcript_bytes > 0 or prefill_source_bytes > 0,
            snapshots_included,
            redacted_transcript,
        ),
    }


def print_bundle_report(report: JSONDict) -> None:
    content = report["content"]
    integrity = report["integrity"]
    policy = report["share_policy"]
    print(f"bundle: {report['bundle']}")
    print(f"thread: {report.get('thread_id')}")
    print(f"size: {format_bytes(int(report['size_bytes']))}")
    print(f"sha256: {report['sha256']}")
    print(f"classification: {policy['classification']}")
    print(f"trusted transport required: {'yes' if policy['trusted_transport_required'] else 'no'}")
    print(f"transcript content: {'yes' if content['transcript_included'] else 'no'}")
    print(f"prefill source content: {'yes' if content['prefill_sources_included'] else 'no'}")
    print(f"snapshots included: {'yes' if content['snapshots_included'] else 'no'}")
    print(f"redacted transcript: {'yes' if report['redacted_transcript'] else 'no'}")
    print(f"signature: {'present' if integrity['signature_present'] else 'absent'}")
    if integrity.get("signature_key_id"):
        print(f"signature key id: {integrity['signature_key_id']}")
    print(f"encrypted: {'yes' if integrity['encrypted'] else 'no'}")
    duplicates = report.get("entries", {}).get("duplicate_entries", [])
    if duplicates:
        print(f"duplicate entries: {len(duplicates)}")
    for warning in policy.get("warnings", []):
        print(f"warning: {warning}")
    for recommendation in policy.get("recommendations", []):
        print(f"recommendation: {recommendation}")


BUNDLE_POLICY_PRESETS: dict[str, set[str]] = {
    "report": set(),
    "metadata-only": {"disallow_plaintext", "disallow_snapshots"},
    "signed-metadata-only": {"disallow_plaintext", "disallow_snapshots", "require_signature"},
    "sealed": {"require_encryption"},
}


def bundle_policy_requirements(
    preset: str,
    disallow_plaintext: bool = False,
    disallow_snapshots: bool = False,
    require_signature: bool = False,
    require_encryption: bool = False,
    require_digest_index: bool = False,
) -> set[str]:
    if preset not in BUNDLE_POLICY_PRESETS:
        allowed = ", ".join(sorted(BUNDLE_POLICY_PRESETS))
        raise RuntimeError(f"Unsupported bundle policy preset: {preset}. Allowed: {allowed}")
    requirements = set(BUNDLE_POLICY_PRESETS[preset])
    if disallow_plaintext:
        requirements.add("disallow_plaintext")
    if disallow_snapshots:
        requirements.add("disallow_snapshots")
    if require_signature:
        requirements.add("require_signature")
    if require_encryption:
        requirements.add("require_encryption")
    if require_digest_index:
        requirements.add("require_digest_index")
    return requirements


def evaluate_bundle_policy(
    report: JSONDict,
    preset: str,
    disallow_plaintext: bool = False,
    disallow_snapshots: bool = False,
    require_signature: bool = False,
    require_encryption: bool = False,
    require_digest_index: bool = False,
) -> JSONDict:
    requirements = bundle_policy_requirements(
        preset,
        disallow_plaintext,
        disallow_snapshots,
        require_signature,
        require_encryption,
        require_digest_index,
    )
    content = report["content"]
    integrity = report["integrity"]
    entries = report["entries"]
    failures: list[str] = []
    if entries.get("duplicate_entries"):
        failures.append("duplicate bundle entries are present")
    if "disallow_plaintext" in requirements and (
        content.get("transcript_included") or content.get("prefill_sources_included")
    ):
        failures.append("plaintext transcript or prefill source content is present")
    if "disallow_snapshots" in requirements and content.get("snapshots_included"):
        failures.append("hard snapshot blobs are included")
    if "require_signature" in requirements and not integrity.get("signature_present"):
        failures.append("bundle signature is absent")
    if "require_encryption" in requirements and not integrity.get("encrypted"):
        failures.append("bundle encryption is absent")
    if "require_digest_index" in requirements and not integrity.get("file_digest_index_present"):
        failures.append("file digest index is absent")
    return {
        "bundle": report["bundle"],
        "thread_id": report.get("thread_id"),
        "preset": preset,
        "requirements": sorted(requirements),
        "classification": report["share_policy"]["classification"],
        "passed": not failures,
        "failures": failures,
        "share_policy": report["share_policy"],
        "content": content,
        "integrity": integrity,
    }


def print_bundle_policy_result(result: JSONDict) -> None:
    print(f"bundle: {result['bundle']}")
    print(f"thread: {result.get('thread_id')}")
    print(f"policy preset: {result['preset']}")
    print(f"classification: {result['classification']}")
    requirements = result.get("requirements", [])
    print(f"requirements: {', '.join(requirements) if requirements else 'report-only'}")
    print(f"policy passed: {'yes' if result['passed'] else 'no'}")
    for failure in result.get("failures", []):
        print(f"failure: {failure}")
    for warning in result.get("share_policy", {}).get("warnings", []):
        print(f"warning: {warning}")


def bundle_policy_command(args: argparse.Namespace) -> int:
    report = inspect_bundle_report(args.bundle.resolve())
    result = evaluate_bundle_policy(
        report,
        args.preset,
        args.disallow_plaintext,
        args.disallow_snapshots,
        args.require_signature,
        args.require_encryption,
        args.require_digest_index,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_bundle_policy_result(result)
    return 0 if result["passed"] else 1


def enforce_bundle_policy(
    bundle_path: Path,
    preset: str = "report",
    disallow_plaintext: bool = False,
    disallow_snapshots: bool = False,
    require_signature: bool = False,
    require_encryption: bool = False,
    require_digest_index: bool = False,
) -> JSONDict:
    result = evaluate_bundle_policy(
        inspect_bundle_report(bundle_path),
        preset,
        disallow_plaintext,
        disallow_snapshots,
        require_signature,
        require_encryption,
        require_digest_index,
    )
    if not result["passed"]:
        joined = "; ".join(result["failures"])
        raise RuntimeError(f"Bundle policy failed ({preset}): {joined}")
    return result


def enforce_bundle_policy_from_args(bundle_path: Path, args: argparse.Namespace) -> None:
    enforce_bundle_policy(
        bundle_path,
        getattr(args, "policy_preset", "report"),
        bool(getattr(args, "disallow_plaintext", False)),
        bool(getattr(args, "disallow_snapshots", False)),
        bool(getattr(args, "require_signature", False)),
        bool(getattr(args, "require_encryption", False)),
        bool(getattr(args, "require_digest_index", False)),
    )


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


def rewrite_thread_ref(ref: str, source_thread: str, target_thread: str) -> str:
    normalized = safe_zip_name(ref)
    parts = Path(normalized).parts
    if len(parts) >= 2 and parts[0] == "threads":
        if parts[1] != source_thread:
            raise RuntimeError(f"Unexpected thread ref in bundle: {ref}")
        if source_thread != target_thread:
            path = Path("threads") / target_thread
            for part in parts[2:]:
                path /= part
            return path.as_posix()
    return normalized


def rewrite_import_json(data: JSONDict, store: Store, source_thread: str, target_thread: str) -> JSONDict:
    if data.get("thread_id") == source_thread:
        data["thread_id"] = target_thread
    if isinstance(data.get("transcript_ref"), str):
        data["transcript_ref"] = rewrite_thread_ref(data["transcript_ref"], source_thread, target_thread)

    for link in data.get("capsules", []):
        if isinstance(link, dict) and isinstance(link.get("manifest_ref"), str):
            link["manifest_ref"] = rewrite_thread_ref(link["manifest_ref"], source_thread, target_thread)

    for diff in data.get("open_diffs", []):
        if isinstance(diff, dict) and isinstance(diff.get("transcript_ref"), str):
            diff["transcript_ref"] = rewrite_thread_ref(diff["transcript_ref"], source_thread, target_thread)

    storage = data.get("storage")
    if isinstance(storage, dict):
        snapshot_ref = storage.get("snapshot_ref")
        if isinstance(snapshot_ref, str) and snapshot_ref:
            storage["snapshot_ref"] = rewrite_thread_ref(snapshot_ref, source_thread, target_thread)
            if storage.get("mode") == "local_file":
                storage["runtime_snapshot_ref"] = str((store.root / storage["snapshot_ref"]).resolve())

    return data


def imported_entry_target(name: str, source_thread: str, target_thread: str) -> str | None:
    if not (name.startswith("endpoints/") or name.startswith("prefills/") or name.startswith("threads/")):
        return None
    parts = Path(name).parts
    if parts[0] == "threads":
        if len(parts) < 2 or parts[1] != source_thread:
            raise RuntimeError(f"Unexpected thread path in bundle: {name}")
        if source_thread != target_thread:
            path = Path("threads") / target_thread
            for part in parts[2:]:
                path /= part
            return path.as_posix()
    return name


def imported_entry_bytes(
    bundle: zipfile.ZipFile,
    item: zipfile.ZipInfo,
    target_name: str,
    store: Store,
    source_thread: str,
    target_thread: str,
) -> bytes:
    payload = bundle.read(item.filename)
    if not target_name.endswith(".json"):
        return payload
    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return payload
    if not isinstance(data, dict):
        return payload
    return pretty_json_bytes(rewrite_import_json(data, store, source_thread, target_thread))


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
        "redaction": {
            "transcript": bool(args.redact_transcript),
            "prefill_sources": bool(args.redact_transcript),
            "policy": "metadata_only" if args.redact_transcript else "none",
        },
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
        if bundle_manifest.get("bundle_type") == "session-capsules.sealed":
            raise RuntimeError("Sealed bundles must be unsealed before import")
        source_thread_id = str(bundle_manifest["thread_id"])
        thread_id = slugify(str(args.thread_id)) if args.thread_id else source_thread_id
        target_ledger = store.ledger_path(thread_id)
        if target_ledger.exists() and not args.force:
            raise FileExistsError(f"Thread already exists: {thread_id}")
        compatibility_warnings = import_compatibility_warnings(store, bundle)

        extracted = 0
        for item in bundle.infolist():
            if item.is_dir():
                continue
            name = safe_zip_name(item.filename)
            target_name = imported_entry_target(name, source_thread_id, thread_id)
            if target_name is None:
                continue
            target = store.root / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(imported_entry_bytes(bundle, item, target_name, store, source_thread_id, thread_id))
            extracted += 1

    print(f"imported bundle: {bundle_path}")
    print(f"thread: {thread_id}")
    if thread_id != bundle_manifest["thread_id"]:
        print(f"source thread: {bundle_manifest['thread_id']}")
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


def run_external_crypto(command: list[str]) -> None:
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"External encryption command failed: {message}")


def age_recipient_from_args(args: argparse.Namespace) -> tuple[str, str | None]:
    inline_recipient = getattr(args, "age_recipient", None)
    recipient_file = getattr(args, "age_recipient_file", None)
    if inline_recipient and recipient_file:
        raise RuntimeError("Use only one age recipient source: --age-recipient or --age-recipient-file")
    if recipient_file:
        recipient_path = Path(recipient_file)
        recipient = recipient_path.read_text(encoding="utf-8").strip()
        if not recipient:
            raise RuntimeError(f"age recipient file is empty: {recipient_path}")
        return recipient, str(recipient_path)
    if inline_recipient:
        recipient = str(inline_recipient).strip()
        if recipient:
            return recipient, None
    raise RuntimeError("Seal requires --age-recipient or --age-recipient-file")


def seal_bundle(args: argparse.Namespace) -> int:
    source = args.bundle.resolve()
    out_path = args.out.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Bundle not found: {source}")
    source_integrity = verify_bundle_integrity(source)
    if not source_integrity["verified"]:
        raise RuntimeError(f"Bundle integrity verification failed: {source_integrity.get('reason')}")
    if out_path.exists() and not args.force:
        raise FileExistsError(f"Sealed bundle already exists: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    age_recipient, age_recipient_file = age_recipient_from_args(args)

    with tempfile.TemporaryDirectory(prefix="session-capsules-seal-") as temp:
        encrypted_payload = Path(temp) / "payload.scap.age"
        run_external_crypto(
            [
                args.age_bin,
                "-r",
                age_recipient,
                "-o",
                str(encrypted_payload),
                str(source),
            ]
        )
        manifest: JSONDict = {
            "schema_version": "0.1",
            "bundle_type": "session-capsules.sealed",
            "created_at": now_iso(),
            "sealed_format": "age-payload-v0",
            "payload_ref": "payload.scap.age",
            "source_bundle_bytes": source.stat().st_size,
            "source_bundle_sha256": digest_file(source),
            "integrity": {
                "file_digest_algorithm": "sha256",
                "signature": None,
                "encryption": {
                    "backend": "age",
                    "mode": "recipient",
                    "payload_ref": "payload.scap.age",
                    "recipient": age_recipient,
                    "recipient_source": "file" if age_recipient_file else "inline",
                    "recipient_file": age_recipient_file,
                    "source_bundle_sha256": digest_file(source),
                },
                "notes": [
                    "Payload encryption is delegated to an external age-compatible command.",
                    "Model weights are never included in sealed bundles.",
                ],
            },
            "file_digests": {
                "payload.scap.age": digest_file(encrypted_payload),
            },
            "notes": [
                "Unseal before import; sealed envelopes are not imported directly.",
            ],
        }
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as sealed:
            add_text_to_zip(sealed, "manifest.json", json.dumps(manifest, indent=2) + "\n")
            add_file_to_zip(sealed, encrypted_payload, "payload.scap.age")

    print(f"sealed bundle: {out_path}")
    print(f"source: {source}")
    print(f"backend: age")
    print(f"sealed bytes: {out_path.stat().st_size}")
    print(f"sealed size: {format_bytes(out_path.stat().st_size)}")
    return 0


def sealed_bundle_manifest(sealed_path: Path) -> JSONDict:
    if not sealed_path.exists():
        raise FileNotFoundError(f"Sealed bundle not found: {sealed_path}")
    integrity = verify_bundle_integrity(sealed_path)
    if not integrity["verified"]:
        raise RuntimeError(f"Sealed bundle integrity verification failed: {integrity.get('reason')}")
    with zipfile.ZipFile(sealed_path, "r") as sealed:
        manifest = json.loads(sealed.read("manifest.json").decode("utf-8"))
    if manifest.get("bundle_type") != "session-capsules.sealed":
        raise RuntimeError("Bundle is not a sealed envelope")
    integrity = manifest.get("integrity", {})
    encryption = integrity.get("encryption") if isinstance(integrity, dict) else None
    if not isinstance(encryption, dict) or encryption.get("backend") != "age":
        raise RuntimeError("Sealed bundle does not declare an age encryption backend")
    return manifest


def unseal_bundle(args: argparse.Namespace) -> int:
    sealed_path = args.bundle.resolve()
    out_path = args.out.resolve()
    manifest = sealed_bundle_manifest(sealed_path)
    if out_path.exists() and not args.force:
        raise FileExistsError(f"Output bundle already exists: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload_ref = safe_zip_name(str(manifest.get("payload_ref") or "payload.scap.age"))

    with tempfile.TemporaryDirectory(prefix="session-capsules-unseal-") as temp:
        encrypted_payload = Path(temp) / "payload.scap.age"
        with zipfile.ZipFile(sealed_path, "r") as sealed:
            encrypted_payload.write_bytes(sealed.read(payload_ref))
        run_external_crypto(
            [
                args.age_bin,
                "-d",
                "-i",
                str(args.age_identity),
                "-o",
                str(out_path),
                str(encrypted_payload),
            ]
        )

    expected_digest = manifest.get("source_bundle_sha256")
    actual_digest = digest_file(out_path)
    if expected_digest and actual_digest != expected_digest:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unsealed bundle digest mismatch: expected {expected_digest}, got {actual_digest}")
    output_integrity = verify_bundle_integrity(out_path)
    if not output_integrity["verified"]:
        raise RuntimeError(f"Unsealed bundle integrity verification failed: {output_integrity.get('reason')}")
    print(f"unsealed bundle: {out_path}")
    print(f"source sealed bundle: {sealed_path}")
    print(f"sha256: {actual_digest}")
    return 0


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


def opencode_thread_id(workspace: str, session: str | None) -> str:
    seed = workspace if not session else f"{workspace}\n{session}"
    return "opencode-" + digest_text(seed).split(":", 1)[1][:12]


def normalize_openai_base_url(value: str) -> str:
    base = value.rstrip("/")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def opencode_config_payload(args: argparse.Namespace) -> JSONDict:
    workspace = str(Path(args.workspace).resolve()) if args.workspace else str(Path.cwd().resolve())
    session = str(args.session).strip() if args.session else None
    thread = slugify(args.thread) if args.thread else opencode_thread_id(workspace, session)
    provider_id = slugify(args.provider_id)
    model_id = slugify(args.model_id)
    model_ref = f"{provider_id}/{model_id}"
    headers: JSONDict = {
        "X-Capsule-Workspace": workspace,
        "X-Capsule-Thread": thread,
    }
    if args.prefill:
        headers["X-Capsule-Prefill"] = slugify(args.prefill)
    config: JSONDict = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Session Capsules",
                "options": {
                    "baseURL": normalize_openai_base_url(args.gateway_url),
                    "apiKey": f"{{env:{args.gateway_token_env}}}",
                    "headers": headers,
                },
                "models": {
                    model_id: {
                        "name": args.model_name,
                    }
                },
            }
        },
        "model": model_ref,
    }
    return {
        "schema_version": "0.1",
        "integration_type": "opencode_config",
        "workspace": workspace,
        "session": session,
        "thread": thread,
        "prefill": slugify(args.prefill) if args.prefill else None,
        "openai_base_url": normalize_openai_base_url(args.gateway_url),
        "gateway_token_env": args.gateway_token_env,
        "config": config,
        "command": ["opencode", "--model", model_ref],
    }


def integration_opencode_config(args: argparse.Namespace) -> int:
    payload = opencode_config_payload(args)
    if args.out:
        write_json(args.out, payload["config"])
        payload["out"] = str(args.out)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if args.out:
        print(f"wrote opencode config: {args.out}")
    print(f"thread: {payload['thread']}")
    print(f"workspace: {payload['workspace']}")
    print(f"openai_base_url: {payload['openai_base_url']}")
    print(f"gateway_token_env: {payload['gateway_token_env']}")
    print("command:")
    print(subprocess.list2cmdline(payload["command"]))
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
        "gateway_store_bundle",
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


def reject_secret_values(name: str, value: Any) -> None:
    secret_value_keys = {"value", "token_value", "key_value", "secret", "secret_value"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in secret_value_keys:
                raise RuntimeError(f"{name} must contain secret references only, not secret values: {key}")
            reject_secret_values(f"{name}.{key}", item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_values(f"{name}[{index}]", item)


def require_profile_section(profile: JSONDict, key: str) -> JSONDict:
    section = profile.get(key)
    if not isinstance(section, dict):
        raise RuntimeError(f"Gateway launch profile section must be an object: {key}")
    return section


TRANSPORT_CAPABILITY_NAMES = {
    "export",
    "list",
    "download",
    "store_upload",
    "raw_upload_import",
    "stored_bundle_import",
    "delete",
    "thread_id_override",
    "digest_verification",
    "hmac_sha256_signing",
    "require_signature_on_import",
    "bundle_policy_gate",
}

DEFAULT_MODEL_PLANE_REQUIRED_CAPABILITIES = [
    "export",
    "list",
    "download",
    "store_upload",
    "raw_upload_import",
    "stored_bundle_import",
    "delete",
    "thread_id_override",
    "bundle_policy_gate",
]


def profile_required_transport_capabilities(transport: JSONDict) -> list[str]:
    raw = transport.get("required_capabilities", DEFAULT_MODEL_PLANE_REQUIRED_CAPABILITIES)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError("Gateway launch profile transport.required_capabilities must be a string array")
    unknown = sorted(set(raw) - TRANSPORT_CAPABILITY_NAMES)
    if unknown:
        allowed = ", ".join(sorted(TRANSPORT_CAPABILITY_NAMES))
        raise RuntimeError(
            "Gateway launch profile transport.required_capabilities contains unsupported values: "
            f"{', '.join(unknown)}. Allowed: {allowed}"
        )
    if len(raw) != len(set(raw)):
        raise RuntimeError("Gateway launch profile transport.required_capabilities must not contain duplicates")
    return raw


def secret_ref_args(name: str, secret_ref: JSONDict, file_flag: str, env_flag: str) -> list[str]:
    source = secret_ref.get("source")
    ref = secret_ref.get("ref")
    if source == "none":
        if ref is not None:
            raise RuntimeError(f"{name}.ref must be null when source is none")
        return []
    if source == "file":
        if not isinstance(ref, str) or not ref:
            raise RuntimeError(f"{name}.ref must be a non-empty file path")
        return [file_flag, ref]
    if source == "env":
        if not isinstance(ref, str) or not ref:
            raise RuntimeError(f"{name}.ref must be a non-empty environment variable name")
        return [env_flag, ref]
    raise RuntimeError(f"{name}.source must be none, file, or env")


def profile_bundle_import_policy(security: JSONDict, bundle_signing: JSONDict) -> JSONDict:
    policy = security.get("bundle_import_policy") or {}
    if not isinstance(policy, dict):
        raise RuntimeError("Gateway launch profile security.bundle_import_policy must be an object")
    preset = str(policy.get("preset", "report"))
    disallow_plaintext = bool(policy.get("disallow_plaintext", False))
    disallow_snapshots = bool(policy.get("disallow_snapshots", False))
    require_encryption = bool(policy.get("require_encryption", False))
    require_digest_index = bool(policy.get("require_digest_index", False))
    requirements = bundle_policy_requirements(
        preset,
        disallow_plaintext,
        disallow_snapshots,
        bool(bundle_signing.get("require_on_import", False)),
        require_encryption,
        require_digest_index,
    )
    if "require_signature" in requirements and not bundle_signing.get("require_on_import"):
        raise RuntimeError(
            "Gateway launch profile bundle import policy requires signatures; "
            "set security.bundle_signing.require_on_import=true so the gateway verifies them"
        )
    return {
        "preset": preset,
        "requirements": sorted(requirements),
        "disallow_plaintext": disallow_plaintext,
        "disallow_snapshots": disallow_snapshots,
        "require_signature": "require_signature" in requirements,
        "verify_signature": bool(bundle_signing.get("require_on_import", False)),
        "require_encryption": require_encryption,
        "require_digest_index": require_digest_index,
    }


def profile_bundle_sealing(security: JSONDict) -> JSONDict:
    sealing = security.get("bundle_sealing") or {}
    if not isinstance(sealing, dict):
        raise RuntimeError("Gateway launch profile security.bundle_sealing must be an object")
    if not sealing:
        return {
            "enabled": False,
            "age_bin": None,
            "age_recipient_file": None,
            "require_for_external_transfer": False,
            "policy_preset": None,
            "seal_command_template": None,
        }

    allowed_keys = {"enabled", "age_bin", "age_recipient_file", "require_for_external_transfer"}
    unknown = sorted(set(sealing) - allowed_keys)
    if unknown:
        raise RuntimeError("Gateway launch profile security.bundle_sealing has unsupported keys: " + ", ".join(unknown))
    missing = sorted(allowed_keys - set(sealing))
    if missing:
        raise RuntimeError("Gateway launch profile security.bundle_sealing is missing required keys: " + ", ".join(missing))
    enabled = sealing["enabled"]
    require_for_external_transfer = sealing["require_for_external_transfer"]
    if not isinstance(enabled, bool):
        raise RuntimeError("Gateway launch profile security.bundle_sealing.enabled must be a boolean")
    if not isinstance(require_for_external_transfer, bool):
        raise RuntimeError("Gateway launch profile security.bundle_sealing.require_for_external_transfer must be a boolean")
    age_bin = str(sealing.get("age_bin") or "age").strip()
    age_recipient_file = sealing.get("age_recipient_file")
    if require_for_external_transfer and not enabled:
        raise RuntimeError("Gateway launch profile cannot require sealed external transfer when bundle_sealing.enabled=false")
    if enabled:
        if not age_bin:
            raise RuntimeError("Gateway launch profile security.bundle_sealing.age_bin must be a non-empty string")
        if not isinstance(age_recipient_file, str) or not age_recipient_file.strip():
            raise RuntimeError(
                "Gateway launch profile security.bundle_sealing.age_recipient_file must be a non-empty public file reference"
            )
        age_recipient_file = age_recipient_file.strip()
    elif age_recipient_file is not None:
        raise RuntimeError("Gateway launch profile security.bundle_sealing.age_recipient_file must be null when disabled")

    seal_template = None
    if enabled:
        seal_template = [
            "py",
            "-3",
            "scripts/capsule_cli.py",
            "seal",
            "{bundle}",
            "--out",
            "{sealed_bundle}",
            "--age-bin",
            age_bin,
            "--age-recipient-file",
            age_recipient_file,
        ]
    return {
        "enabled": enabled,
        "age_bin": age_bin if enabled else None,
        "age_recipient_file": age_recipient_file,
        "require_for_external_transfer": require_for_external_transfer,
        "policy_preset": "sealed" if require_for_external_transfer else None,
        "seal_command_template": seal_template,
    }


def gateway_launch_args(profile: JSONDict) -> list[str]:
    if profile.get("schema_version") != "0.1":
        raise RuntimeError("Gateway launch profile schema_version must be 0.1")
    if profile.get("profile_type") != "session_capsule_gateway":
        raise RuntimeError("Gateway launch profile profile_type must be session_capsule_gateway")
    reject_secret_values("gateway launch profile", profile)

    command = profile.get("command")
    if isinstance(command, dict):
        program = str(command.get("program") or "").strip()
        prefix = [program] if program else []
        raw_args = command.get("args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
            raise RuntimeError("Gateway launch profile command.args must be a string array")
        prefix.extend(raw_args)
    else:
        prefix = [sys.executable, str(Path(__file__).with_name("capsule_gateway.py"))]

    gateway = require_profile_section(profile, "gateway")
    launch = [
        *prefix,
        "--state-dir",
        str(gateway["state_dir"]),
        "--endpoint",
        str(gateway["endpoint_id"]),
        "--host",
        str(gateway["host"]),
        "--port",
        str(int(gateway["port"])),
        "--checkpoint-mode",
        str(gateway["checkpoint_mode"]),
        "--slot",
        str(int(gateway["slot"])),
        "--timeout",
        str(gateway["timeout_seconds"]),
        "--max-bundle-bytes",
        str(gateway["max_bundle_bytes"]),
    ]
    if gateway.get("default_prefill"):
        launch.extend(["--default-prefill", str(gateway["default_prefill"])])
    if gateway.get("default_thread_prefix"):
        launch.extend(["--default-thread-prefix", str(gateway["default_thread_prefix"])])
    if gateway.get("cors_allow_origin"):
        launch.extend(["--cors-allow-origin", str(gateway["cors_allow_origin"])])

    security = require_profile_section(profile, "security")
    request_auth = security.get("request_auth")
    if not isinstance(request_auth, dict):
        raise RuntimeError("Gateway launch profile security.request_auth must be an object")
    launch.extend(secret_ref_args("security.request_auth", request_auth, "--auth-token-file", "--auth-token-env"))

    bundle_signing = security.get("bundle_signing")
    if not isinstance(bundle_signing, dict):
        raise RuntimeError("Gateway launch profile security.bundle_signing must be an object")
    if bundle_signing.get("source") == "none" and bundle_signing.get("require_on_import"):
        raise RuntimeError("Gateway launch profile cannot require signed imports without a bundle signing key source")
    launch.extend(secret_ref_args("security.bundle_signing", bundle_signing, "--signature-key-file", "--signature-key-env"))
    if bundle_signing.get("key_id"):
        launch.extend(["--signature-key-id", str(bundle_signing["key_id"])])
    if bundle_signing.get("require_on_import"):
        launch.append("--require-bundle-signature")
    import_policy = profile_bundle_import_policy(security, bundle_signing)
    launch.extend(["--bundle-policy-preset", import_policy["preset"]])
    if import_policy["disallow_plaintext"]:
        launch.append("--bundle-policy-disallow-plaintext")
    if import_policy["disallow_snapshots"]:
        launch.append("--bundle-policy-disallow-snapshots")
    if import_policy["require_encryption"]:
        launch.append("--bundle-policy-require-encryption")
    if import_policy["require_digest_index"]:
        launch.append("--bundle-policy-require-digest-index")
    return launch


def render_gateway_command(args: argparse.Namespace) -> int:
    profile_path = args.profile.resolve()
    profile = read_json(profile_path)
    launch = gateway_launch_args(profile)
    transport = require_profile_section(profile, "transport")
    security = require_profile_section(profile, "security")
    required_capabilities = profile_required_transport_capabilities(transport)
    output = {
        "profile_id": profile.get("profile_id"),
        "profile": str(profile_path),
        "command": launch,
        "command_line": subprocess.list2cmdline(launch),
        "openai_base_url": transport.get("openai_base_url"),
        "status_url": transport.get("status_url"),
        "require_status_transport": bool(transport.get("require_status_transport", False)),
        "required_capabilities": required_capabilities,
        "bundle_sealing": profile_bundle_sealing(security),
    }
    if args.json:
        print(json.dumps(output, indent=2) + "\n", end="")
        return 0
    print(f"profile: {output['profile_id']}")
    print(f"openai_base_url: {output['openai_base_url']}")
    print(f"status_url: {output['status_url']}")
    print(f"require_status_transport: {str(output['require_status_transport']).lower()}")
    print(f"required_capabilities: {', '.join(required_capabilities) if required_capabilities else 'none'}")
    print("command:")
    print(output["command_line"])
    return 0


def resolve_profile_ref(profile_dir: Path, ref: str) -> Path:
    path = Path(ref)
    if path.is_absolute():
        return path
    return profile_dir / path


def read_profile_secret_token(name: str, secret_ref: JSONDict, profile_dir: Path) -> str | None:
    source = secret_ref.get("source")
    ref = secret_ref.get("ref")
    if source == "none":
        if ref is not None:
            raise RuntimeError(f"{name}.ref must be null when source is none")
        return None
    if source == "file":
        if not isinstance(ref, str) or not ref:
            raise RuntimeError(f"{name}.ref must be a non-empty file path")
        token = resolve_profile_ref(profile_dir, ref).read_text(encoding="utf-8").strip()
    elif source == "env":
        if not isinstance(ref, str) or not ref:
            raise RuntimeError(f"{name}.ref must be a non-empty environment variable name")
        token = os.environ.get(ref, "").strip()
    else:
        raise RuntimeError(f"{name}.source must be none, file, or env")
    if not token:
        raise RuntimeError(f"{name} resolved to an empty token")
    return token


def gateway_profile_auth_headers(profile: JSONDict, profile_dir: Path) -> dict[str, str]:
    security = require_profile_section(profile, "security")
    request_auth = security.get("request_auth")
    if not isinstance(request_auth, dict):
        raise RuntimeError("Gateway launch profile security.request_auth must be an object")
    token = read_profile_secret_token("security.request_auth", request_auth, profile_dir)
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


def verify_gateway_status_contract(profile: JSONDict, status: JSONDict) -> JSONDict:
    gateway = require_profile_section(profile, "gateway")
    transport = require_profile_section(profile, "transport")
    security = require_profile_section(profile, "security")
    request_auth = security.get("request_auth")
    bundle_signing = security.get("bundle_signing")
    if not isinstance(request_auth, dict):
        raise RuntimeError("Gateway launch profile security.request_auth must be an object")
    if not isinstance(bundle_signing, dict):
        raise RuntimeError("Gateway launch profile security.bundle_signing must be an object")

    expected_auth_required = request_auth.get("source") != "none"
    expected_signing = bundle_signing.get("source") != "none"
    expected_upload_bytes = parse_bytes(str(gateway["max_bundle_bytes"]))
    expected_cors_origin = gateway.get("cors_allow_origin")
    expected_import_policy = profile_bundle_import_policy(security, bundle_signing)
    required_capabilities = profile_required_transport_capabilities(transport)

    mismatches: list[str] = []
    checks = {
        "status_ok": status.get("status") == "ok",
        "endpoint_id": status.get("endpoint_id") == gateway.get("endpoint_id"),
        "checkpoint_mode": status.get("checkpoint_mode") == gateway.get("checkpoint_mode"),
        "auth_required": status.get("auth_required") == expected_auth_required,
    }
    for name, passed in checks.items():
        if not passed:
            mismatches.append(name)

    transport_status = status.get("transport")
    transport_verified = True
    transport_capabilities: dict[str, Any] = {}
    if transport.get("require_status_transport", False):
        if not isinstance(transport_status, dict):
            raise RuntimeError("Gateway status response did not include required transport object")
        transport_checks = {
            "transport.api_version": transport_status.get("api_version") == "0.1",
            "transport.content_type": transport_status.get("bundle_content_type") == "application/vnd.session-capsule.scap",
            "transport.max_upload_bytes": transport_status.get("max_upload_bytes") == expected_upload_bytes,
            "transport.auth.required": transport_status.get("auth", {}).get("required") == expected_auth_required,
            "transport.signing.exports_signed": transport_status.get("signing", {}).get("exports_signed") == expected_signing,
            "transport.signing.required_on_import": transport_status.get("signing", {}).get("required_on_import")
            == bool(bundle_signing.get("require_on_import", False)),
        }
        cors_status = transport_status.get("cors", {})
        if expected_cors_origin:
            transport_checks["transport.cors.enabled"] = cors_status.get("enabled") is True
            transport_checks["transport.cors.allow_origin"] = cors_status.get("allow_origin") == expected_cors_origin
        import_policy_status = transport_status.get("import_policy", {})
        transport_checks["transport.import_policy.preset"] = import_policy_status.get("preset") == expected_import_policy["preset"]
        transport_checks["transport.import_policy.requirements"] = (
            import_policy_status.get("requirements") == expected_import_policy["requirements"]
        )
        for key in [
            "disallow_plaintext",
            "disallow_snapshots",
            "require_signature",
            "verify_signature",
            "require_encryption",
            "require_digest_index",
        ]:
            transport_checks[f"transport.import_policy.{key}"] = import_policy_status.get(key) == expected_import_policy[key]
        raw_capabilities = transport_status.get("capabilities", {})
        capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else {}
        transport_capabilities = capabilities
        for capability in required_capabilities:
            transport_checks[f"transport.capability.{capability}"] = capabilities.get(capability) is True
        for name, passed in transport_checks.items():
            if not passed:
                mismatches.append(name)
        transport_verified = not any(name.startswith("transport.") for name in mismatches)

    endpoint_compatibility = status.get("endpoint_compatibility")
    endpoint_verified = True
    if gateway.get("checkpoint_mode") == "hard":
        endpoint_verified = (
            isinstance(endpoint_compatibility, dict)
            and endpoint_compatibility.get("hard_checkpoint_ready") is True
            and endpoint_compatibility.get("endpoint_id") == gateway.get("endpoint_id")
        )
        if not endpoint_verified:
            mismatches.append("endpoint_compatibility.hard_checkpoint_ready")

    if mismatches:
        raise RuntimeError("Gateway status did not match launch profile: " + ", ".join(mismatches))

    return {
        "status": status.get("status"),
        "endpoint_id": status.get("endpoint_id"),
        "checkpoint_mode": status.get("checkpoint_mode"),
        "auth_required": status.get("auth_required"),
        "transport_verified": transport_verified,
        "endpoint_verified": endpoint_verified,
        "endpoint_compatibility": endpoint_compatibility,
        "required_capabilities": required_capabilities,
        "transport_capabilities": sorted(key for key, value in transport_capabilities.items() if value is True),
        "threads": status.get("threads"),
        "bundles": status.get("bundles"),
        "bundle_import_policy": transport_status.get("import_policy") if isinstance(transport_status, dict) else None,
    }


def check_gateway_profile(args: argparse.Namespace) -> int:
    profile_path = args.profile.resolve()
    profile = read_json(profile_path)
    gateway_launch_args(profile)
    transport = require_profile_section(profile, "transport")
    gateway = require_profile_section(profile, "gateway")
    status_url = str(transport["status_url"])
    timeout = float(args.timeout if args.timeout is not None else gateway.get("timeout_seconds", 120.0))
    status = gateway_request_json("GET", status_url, None, timeout, gateway_profile_auth_headers(profile, profile_path.parent))
    summary = verify_gateway_status_contract(profile, status)
    output = {
        "profile_id": profile.get("profile_id"),
        "profile": str(profile_path),
        "status_url": status_url,
        **summary,
    }
    if args.json:
        print(json.dumps(output, indent=2) + "\n", end="")
        return 0
    print(f"profile: {output['profile_id']}")
    print(f"status_url: {status_url}")
    print(f"status: {output['status']}")
    print(f"endpoint_id: {output['endpoint_id']}")
    print(f"checkpoint_mode: {output['checkpoint_mode']}")
    print(f"auth_required: {str(output['auth_required']).lower()}")
    print(f"transport_verified: {str(output['transport_verified']).lower()}")
    print(f"endpoint_verified: {str(output['endpoint_verified']).lower()}")
    return 0


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


def gateway_cli_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    token = read_gateway_job_auth_token(
        getattr(args, "auth_token_file", None),
        getattr(args, "auth_token_env", None),
    )
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


def gateway_cli_params(args: argparse.Namespace) -> JSONDict:
    return {"gateway_url": args.url, "timeout": args.timeout}


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


def gateway_download_bundle_file(
    gateway_url: str,
    bundle_id: str,
    out: Path,
    timeout: float,
    auth_headers: dict[str, str],
) -> JSONDict:
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"{gateway_base_url({'gateway_url': gateway_url})}/api/capsules/bundles/{quote(bundle_id)}"
    req = request.Request(url, headers=auth_headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            response_headers = {key: value for key, value in response.headers.items()}
    except error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc
    out.write_bytes(body)
    return {
        "bundle_id": bundle_id,
        "out": str(out),
        "bytes": len(body),
        "sha256": digest_file(out),
        "content_type": response_headers.get("Content-Type"),
        "response_bundle_id": response_headers.get("X-Capsule-Bundle-Id"),
        "response_sha256": response_headers.get("X-Capsule-Bundle-SHA256"),
    }


def gateway_download_bundle(params: JSONDict, job_file: Path, auth_headers: dict[str, str]) -> int:
    bundle_id = str(require_job_param(params, "bundle_id"))
    out = job_path(job_file.parent, str(require_job_param(params, "out")))
    result = gateway_download_bundle_file(
        gateway_base_url(params),
        bundle_id,
        out,
        gateway_timeout(params),
        auth_headers,
    )
    print(f"downloaded bundle: {out}")
    print(f"bundle_id: {bundle_id}")
    print(f"bytes: {result['bytes']}")
    print(f"sha256: {result['sha256']}")
    return 0


def gateway_store_bundle_file(
    gateway_url: str,
    source: Path,
    timeout: float,
    auth_headers: dict[str, str],
    bundle_id: str | None = None,
    force: bool = False,
    content_type: str = "application/vnd.session-capsule.scap",
) -> JSONDict:
    if not source.exists():
        raise FileNotFoundError(f"Bundle not found: {source}")
    headers = dict(auth_headers)
    headers["Content-Type"] = content_type
    if bundle_id:
        headers["X-Capsule-Bundle-Id"] = bundle_id
    if force:
        headers["X-Capsule-Bundle-Force"] = "true"
    req = request.Request(
        f"{gateway_base_url({'gateway_url': gateway_url})}/api/capsules/bundles",
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
    return data


def gateway_store_bundle(params: JSONDict, job_file: Path, auth_headers: dict[str, str]) -> int:
    source = job_path(job_file.parent, str(require_job_param(params, "bundle")))
    enforce_bundle_policy(
        source,
        str(params.get("policy_preset", "report")),
        bool(params.get("disallow_plaintext", False)),
        bool(params.get("disallow_snapshots", False)),
        bool(params.get("require_signature", False)),
        bool(params.get("require_encryption", False)),
        bool(params.get("require_digest_index", False)),
    )
    data = gateway_store_bundle_file(
        gateway_base_url(params),
        source,
        gateway_timeout(params),
        auth_headers,
        str(params["bundle_id"]) if params.get("bundle_id") else None,
        bool(params.get("force", False)),
        str(params.get("content_type") or "application/vnd.session-capsule.scap"),
    )
    print(json.dumps(data, indent=2))
    return 0


def gateway_status_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    data = gateway_request_json(
        "GET",
        f"{gateway_base_url(params)}/api/capsules/status",
        None,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    transport = data.get("transport", {})
    auth = transport.get("auth", {}) if isinstance(transport, dict) else {}
    capabilities = transport.get("capabilities", {}) if isinstance(transport, dict) else {}
    endpoint_compatibility = data.get("endpoint_compatibility", {})
    if not isinstance(endpoint_compatibility, dict):
        endpoint_compatibility = {}
    slot_probe = endpoint_compatibility.get("slot_probe", {})
    if not isinstance(slot_probe, dict):
        slot_probe = {}
    print(f"gateway: {gateway_base_url(params)}")
    print(f"status: {data.get('status')}")
    print(f"endpoint: {data.get('endpoint_id')}")
    print(f"hard checkpoint ready: {'yes' if endpoint_compatibility.get('hard_checkpoint_ready') else 'no'}")
    print(f"slot probe: {slot_probe.get('status')}")
    print(f"threads: {data.get('threads')}")
    print(f"bundles: {data.get('bundles')}")
    print(f"auth required: {'yes' if auth.get('required') else 'no'}")
    print(f"download: {'yes' if capabilities.get('download') else 'no'}")
    print(f"store upload: {'yes' if capabilities.get('store_upload') else 'no'}")
    print(f"raw upload import: {'yes' if capabilities.get('raw_upload_import') else 'no'}")
    print(f"max upload bytes: {transport.get('max_upload_bytes') if isinstance(transport, dict) else None}")
    return 0


def gateway_list_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    data = gateway_request_json(
        "GET",
        f"{gateway_base_url(params)}/api/capsules/bundles",
        None,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    print(json.dumps(data, indent=2))
    return 0


def gateway_export_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    payload: JSONDict = {
        "thread_id": args.thread,
        "include_snapshots": bool(args.include_snapshots),
        "redact_transcript": bool(args.redact_transcript),
        "force": bool(args.force),
    }
    if args.bundle_id:
        payload["bundle_id"] = args.bundle_id
    data = gateway_request_json(
        "POST",
        f"{gateway_base_url(params)}/api/capsules/export",
        payload,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    print(json.dumps(data, indent=2))
    return 0


def gateway_download_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    result = gateway_download_bundle_file(
        gateway_base_url(params),
        args.bundle_id,
        args.out.resolve(),
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"downloaded bundle: {result['out']}")
    print(f"bundle_id: {result['bundle_id']}")
    print(f"bytes: {result['bytes']}")
    print(f"sha256: {result['sha256']}")
    return 0


def gateway_store_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    source = args.bundle.resolve()
    enforce_bundle_policy_from_args(source, args)
    data = gateway_store_bundle_file(
        gateway_base_url(params),
        source,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
        args.bundle_id,
        bool(args.force),
        "application/vnd.session-capsule.scap",
    )
    print(json.dumps(data, indent=2))
    return 0


def gateway_upload_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    source = args.bundle.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Bundle not found: {source}")
    enforce_bundle_policy_from_args(source, args)
    headers = gateway_cli_auth_headers(args)
    headers["Content-Type"] = "application/vnd.session-capsule.scap"
    if args.bundle_id:
        headers["X-Capsule-Bundle-Id"] = args.bundle_id
    if args.thread_id:
        headers["X-Capsule-Import-Thread"] = args.thread_id
    if args.force:
        headers["X-Capsule-Import-Force"] = "true"
    req = request.Request(
        f"{gateway_base_url(params)}/api/capsules/import",
        data=source.read_bytes(),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=gateway_timeout(params)) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc
    if not isinstance(data, dict):
        raise RuntimeError("Gateway response was not a JSON object")
    print(json.dumps(data, indent=2))
    return 0


def gateway_import_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    payload: JSONDict = {"bundle_id": args.bundle_id, "force": bool(args.force)}
    if args.thread_id:
        payload["thread_id"] = args.thread_id
    data = gateway_request_json(
        "POST",
        f"{gateway_base_url(params)}/api/capsules/import",
        payload,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    print(json.dumps(data, indent=2))
    return 0


def gateway_delete_command(args: argparse.Namespace) -> int:
    params = gateway_cli_params(args)
    data = gateway_request_json(
        "DELETE",
        f"{gateway_base_url(params)}/api/capsules/bundles/{quote(args.bundle_id)}",
        None,
        gateway_timeout(params),
        gateway_cli_auth_headers(args),
    )
    print(json.dumps(data, indent=2))
    return 0


def add_gateway_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Gateway base URL. /v1 suffixes are accepted.")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--auth-token-file", type=Path, help="Local gateway request token file.")
    parser.add_argument("--auth-token-env", help="Environment variable containing the gateway request token.")


def add_gateway_bundle_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle-id", required=True, help="Gateway bundle id without the .scap suffix.")


def add_gateway_thread_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--thread-id", help="Import under a new local thread id.")
    parser.add_argument("--force", action="store_true", help="Allow replacing an existing local thread.")


def add_gateway_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def add_gateway_export_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle-id", help="Stored bundle id. Defaults to a generated id.")
    parser.add_argument("--include-snapshots", action="store_true", help="Include hard local snapshot blobs.")
    parser.add_argument("--redact-transcript", action="store_true", help="Omit transcript and prefill source text.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing stored bundle with the same id.")


def add_gateway_upload_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle", type=Path, required=True, help="Local .scap file to upload and import.")
    parser.add_argument("--bundle-id", help="Stored bundle id for the uploaded .scap.")
    add_gateway_thread_target_args(parser)
    add_bundle_policy_flags(parser, include_json=False, preset_flag="--policy-preset")


def add_gateway_store_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle", type=Path, required=True, help="Local .scap file to store without importing.")
    parser.add_argument("--bundle-id", help="Stored bundle id for the uploaded .scap.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing stored bundle with the same id.")
    add_bundle_policy_flags(parser, include_json=False, preset_flag="--policy-preset")


def add_bundle_policy_flags(
    parser: argparse.ArgumentParser,
    include_json: bool,
    preset_flag: str = "--preset",
) -> None:
    parser.add_argument(
        preset_flag,
        dest="preset" if preset_flag == "--preset" else "policy_preset",
        choices=sorted(BUNDLE_POLICY_PRESETS),
        default="report",
        help="Bundle policy preset to enforce.",
    )
    parser.add_argument("--disallow-plaintext", action="store_true", help="Fail if transcript or prefill source text is present.")
    parser.add_argument("--disallow-snapshots", action="store_true", help="Fail if hard snapshot blobs are present.")
    parser.add_argument("--require-signature", action="store_true", help="Fail unless the bundle has a signature envelope.")
    parser.add_argument("--require-encryption", action="store_true", help="Fail unless the bundle reports encryption.")
    parser.add_argument("--require-digest-index", action="store_true", help="Fail unless the bundle has a file_digests index.")
    if include_json:
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


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
        if params.get("thread_id"):
            headers["X-Capsule-Import-Thread"] = str(params["thread_id"])
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
        if params.get("thread_id"):
            payload["thread_id"] = str(params["thread_id"])
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
    if job_type == "gateway_store_bundle":
        return gateway_store_bundle(params, job_file, auth_headers)
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
    if args.thread and args.bundle:
        raise RuntimeError("Choose only one inspection target: --thread or --bundle")
    if args.bundle:
        report = inspect_bundle_report(args.bundle.resolve())
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_bundle_report(report)
        return 0

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
        if ledger.get("transcript_redacted"):
            print("transcript redacted: yes")
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


def state_info(args: argparse.Namespace) -> int:
    store = Store(args.state_dir)
    state_dir = store.root.resolve()
    cwd = Path.cwd().resolve()
    try:
        relative = state_dir.relative_to(cwd).as_posix()
    except ValueError:
        relative = str(state_dir)
    endpoints = sorted(store.endpoints_dir.glob("*.json")) if store.endpoints_dir.exists() else []
    threads = sorted(store.threads_dir.glob("*/thread-ledger.json")) if store.threads_dir.exists() else []
    print(f"state_dir: {state_dir}")
    print(f"state_ref: {relative}")
    print(f"default_state_dir: .capsules")
    print(f"policy: project_local_default")
    print(f"override: --state-dir")
    print(f"config_path: {store.config_path}")
    print(f"endpoints: {len(endpoints)}")
    print(f"threads: {len(threads)}")
    print("user_level_state: future")
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
    doctor.add_argument("--runtime-metadata-path", default="/props", help="Best-effort endpoint metadata path to probe for runtime build/model/context fields.")
    doctor.add_argument("--skip-runtime-metadata", action="store_true", help="Skip the non-fatal runtime metadata probe.")
    doctor.add_argument("--strict", action="store_true")
    doctor.set_defaults(func=endpoint_doctor)

    matrix = endpoint_sub.add_parser("matrix", help="Summarize endpoint doctor slot probes.")
    matrix.add_argument("--json", action="store_true", help="Print a machine-readable compatibility report.")
    matrix.set_defaults(func=endpoint_matrix)

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

    seal_cmd = subcommands.add_parser("seal", help="Seal a .scap bundle with an external age-compatible encryption command.")
    seal_cmd.add_argument("bundle", type=Path)
    seal_cmd.add_argument("--out", type=Path, required=True)
    seal_cmd.add_argument("--age-recipient", help="age recipient string. This is public key material.")
    seal_cmd.add_argument("--age-recipient-file", type=Path, help="File containing an age recipient string. This is public key material.")
    seal_cmd.add_argument("--age-bin", default="age", help="age-compatible executable to run.")
    seal_cmd.add_argument("--force", action="store_true")
    seal_cmd.set_defaults(func=seal_bundle)

    unseal_cmd = subcommands.add_parser("unseal", help="Unseal an age-encrypted .scap envelope into an importable .scap bundle.")
    unseal_cmd.add_argument("bundle", type=Path)
    unseal_cmd.add_argument("--out", type=Path, required=True)
    unseal_cmd.add_argument("--age-identity", type=Path, required=True, help="age identity file used by the external decrypt command.")
    unseal_cmd.add_argument("--age-bin", default="age", help="age-compatible executable to run.")
    unseal_cmd.add_argument("--force", action="store_true")
    unseal_cmd.set_defaults(func=unseal_bundle)

    policy_cmd = subcommands.add_parser("bundle-policy", help="Check a .scap bundle against share/import policy requirements.")
    policy_cmd.add_argument("bundle", type=Path)
    add_bundle_policy_flags(policy_cmd, include_json=True)
    policy_cmd.set_defaults(func=bundle_policy_command)

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

    gateway_cmd = subcommands.add_parser("gateway")
    gateway_sub = gateway_cmd.add_subparsers(dest="gateway_command", required=True)

    gateway_command = gateway_sub.add_parser("command", help="Render a gateway launch command from a Model Plane profile.")
    gateway_command.add_argument("profile", type=Path)
    gateway_command.add_argument("--json", action="store_true", help="Print a machine-readable command payload.")
    gateway_command.set_defaults(func=render_gateway_command)

    gateway_check = gateway_sub.add_parser("check", help="Check a running gateway against a Model Plane launch profile.")
    gateway_check.add_argument("profile", type=Path)
    gateway_check.add_argument("--json", action="store_true", help="Print a machine-readable status payload.")
    gateway_check.add_argument("--timeout", type=float, help="Override gateway status request timeout.")
    gateway_check.set_defaults(func=check_gateway_profile)

    gateway_status = gateway_sub.add_parser("status", help="Read a running gateway's status and transport contract.")
    add_gateway_client_args(gateway_status)
    add_gateway_json_flag(gateway_status)
    gateway_status.set_defaults(func=gateway_status_command)

    gateway_list = gateway_sub.add_parser("list", help="List stored .scap bundles from a running gateway.")
    add_gateway_client_args(gateway_list)
    gateway_list.set_defaults(func=gateway_list_command)

    gateway_export = gateway_sub.add_parser("export", help="Ask a running gateway to export a thread into a stored bundle.")
    add_gateway_client_args(gateway_export)
    gateway_export.add_argument("--thread", required=True, help="Thread id to export from gateway state.")
    add_gateway_export_flags(gateway_export)
    gateway_export.set_defaults(func=gateway_export_command)

    gateway_download = gateway_sub.add_parser("download", help="Download a stored gateway .scap bundle.")
    add_gateway_client_args(gateway_download)
    add_gateway_bundle_id_arg(gateway_download)
    gateway_download.add_argument("--out", type=Path, required=True)
    add_gateway_json_flag(gateway_download)
    gateway_download.set_defaults(func=gateway_download_command)

    gateway_store = gateway_sub.add_parser("store", help="Upload raw .scap bytes to the gateway bundle store without importing.")
    add_gateway_client_args(gateway_store)
    add_gateway_store_flags(gateway_store)
    gateway_store.set_defaults(func=gateway_store_command)

    gateway_upload = gateway_sub.add_parser("upload", help="Upload raw .scap bytes to a gateway and import them.")
    add_gateway_client_args(gateway_upload)
    add_gateway_upload_flags(gateway_upload)
    gateway_upload.set_defaults(func=gateway_upload_command)

    gateway_import = gateway_sub.add_parser("import", help="Import a bundle already stored by the gateway.")
    add_gateway_client_args(gateway_import)
    add_gateway_bundle_id_arg(gateway_import)
    add_gateway_thread_target_args(gateway_import)
    gateway_import.set_defaults(func=gateway_import_command)

    gateway_delete = gateway_sub.add_parser("delete", help="Delete a stored gateway .scap bundle without deleting imported state.")
    add_gateway_client_args(gateway_delete)
    add_gateway_bundle_id_arg(gateway_delete)
    gateway_delete.set_defaults(func=gateway_delete_command)

    integration_cmd = subcommands.add_parser("integration")
    integration_sub = integration_cmd.add_subparsers(dest="integration_command", required=True)

    opencode_config = integration_sub.add_parser("opencode-config", help="Render an opencode provider config with stable capsule headers.")
    opencode_config.add_argument("--workspace", help="Workspace path or id. Defaults to the current directory.")
    opencode_config.add_argument("--session", help="Optional opencode session id to include in the derived thread id.")
    opencode_config.add_argument("--thread", help="Explicit capsule thread id. Defaults to a workspace/session-derived id.")
    opencode_config.add_argument("--prefill", default="user_default", help="Prefill capsule name to attach on new gateway threads.")
    opencode_config.add_argument("--gateway-url", default="http://127.0.0.1:8765", help="Gateway root URL or /v1 URL.")
    opencode_config.add_argument("--gateway-token-env", default="CAPSULE_GATEWAY_TOKEN", help="Environment variable opencode should read as the API key.")
    opencode_config.add_argument("--provider-id", default="session-capsules")
    opencode_config.add_argument("--model-id", default="fake-model")
    opencode_config.add_argument("--model-name", default="Capsule Gateway Model")
    opencode_config.add_argument("--out", type=Path, help="Write only the opencode provider config JSON to this path.")
    opencode_config.add_argument("--json", action="store_true", help="Print integration metadata plus generated config.")
    opencode_config.set_defaults(func=integration_opencode_config)

    state_cmd = subcommands.add_parser("state")
    state_sub = state_cmd.add_subparsers(dest="state_command", required=True)

    state_info_parser = state_sub.add_parser("info", help="Show capsule state directory policy and paths.")
    state_info_parser.set_defaults(func=state_info)

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
    inspect_cmd.add_argument("--bundle", type=Path)
    inspect_cmd.add_argument("--json", action="store_true")
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
