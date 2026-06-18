# Session Capsule Protocol

## Purpose

The protocol defines how a harness or gateway ties a canonical thread transcript to runtime-specific capsule checkpoints. It is not a model format and it does not transport model weights.

The core relationship is:

```text
thread transcript = canonical human-readable history
capsule manifest  = compatibility and storage metadata for a checkpoint
slot              = temporary runtime placement used only while the model is loaded
```

## Objects

### Endpoint Capabilities

Endpoint capabilities describe what a model endpoint can actually do.

Examples:

- soft checkpoint only
- server-side handle
- local `llama.cpp` slot save/restore
- user-carried blob
- sealed blob
- transcript replay fallback

The first hard local target is a `llama.cpp` endpoint with `/slots` and slot save/restore support.

### Thread Ledger

The thread ledger is the source of truth for a thread. It tracks:

- thread id
- endpoint id
- transcript location
- active capsule id
- capsule chain
- open transcript diffs after the latest checkpoint
- replay fallback policy

On reload, the harness reads the thread ledger first. The slot does not have meaning until the ledger chooses a compatible capsule to restore.

### Capsule Manifest

The capsule manifest describes one checkpoint. It tracks:

- capsule id
- parent capsule id
- thread id
- endpoint id
- model and tokenizer fingerprints
- runtime build and slot format
- token range covered by the checkpoint
- segment index for the covered context
- snapshot storage reference
- optional security metadata

The manifest answers: can this endpoint restore this checkpoint?

### State References

Ledger and manifest references are relative to the capsule state directory selected by `--state-dir`.

Examples:

```text
threads/THREAD/transcript.jsonl
threads/THREAD/manifests/CAPSULE.json
prefills/NAME/VERSION/source.md
threads/THREAD/snapshots/CAPSULE.bin
```

State refs must not be absolute and must not include the `.capsules/` prefix. This keeps ledgers and manifests portable when a project state directory moves or is imported from a `.scap` bundle.

When a bundle is imported under a new local thread id, only thread-owned refs are remapped:

```text
threads/SOURCE/... -> threads/TARGET/...
```

Endpoint records and prefill records remain state-global. Hard local manifests also refresh `storage.runtime_snapshot_ref` to the receiving state directory when a `storage.snapshot_ref` is present.

### Snapshot References

For v0 local hard capsules, `storage.snapshot_ref` is relative to the capsule state directory:

```text
threads/THREAD/snapshots/CAPSULE.bin
```

It follows the same state-reference rule as ledger and manifest refs.

`storage.runtime_snapshot_ref` is separate. It may be an absolute or server-visible path because some runtimes need the exact filename passed to their slot save/restore API.

`storage.snapshot_digest` records content identity and integrity metadata, but v0 does not use content-addressed paths for hard snapshot files. Transcript replay remains the fallback if the local snapshot is missing.

### Bundle Integrity

A `.scap` bundle has its own top-level `manifest.json`. New exports include:

- `integrity.file_digest_algorithm = sha256`
- `file_digests`, covering every zip entry except `manifest.json`
- `integrity.signature = null` or an HMAC-SHA256 signature object
- `integrity.encryption = null`

The digest index detects corrupted, swapped, extra, missing, or duplicate bundle entries. HMAC-SHA256 signing can prove that the verifier and exporter share the same key. It is not public-key identity, and it does not encrypt the bundle.

Redacted exports are metadata-only bundles. They omit transcript and prefill source text, write `transcript_redacted=true` into the thread ledger, set fallback mode to `unavailable_redacted_transcript`, and set `prefill_source.source_redacted=true` when a prefill manifest is included without its source file. Redaction is not encryption; hard snapshots and metadata may still reveal sensitive state.

## Reload Order

Thread reload should happen in this order:

```text
1. Load thread ledger.
2. Pick the latest compatible capsule.
3. Allocate or select a runtime slot.
4. Restore the capsule into that slot.
5. Append transcript diff after capsule.context.token_end.
6. Generate the next response.
7. Save a new checkpoint and update the thread ledger.
```

If restore fails, fallback to transcript replay and write a new checkpoint when the runtime reaches a stable boundary. The CLI marks that capsule `restore_failed` rather than deleting the snapshot, skips it for future automatic restore, replays with `cache_prompt=false`, and saves a replacement checkpoint when `--append-diff` is requested.

The ledger schema allows `restore_failed` capsule links with `last_restore_failed_at`. The manifest lifecycle records `last_restore_failed_at` and `last_restore_error` so the failed runtime path remains auditable without deleting the snapshot blob.

The CLI implements this as:

```powershell
py -3 .\scripts\capsule_cli.py resume --thread THREAD --slot 1 --append-diff
```

Without `--append-diff`, `resume` restores the hard capsule and reports the pending transcript diff range. If restore fails in that mode, it records the fallback plan but does not mutate the runtime slot.

## Full And Diff

The first implementation should use full checkpoint snapshots plus transcript diffs.

```text
full capsule = runtime snapshot covering tokens 0..N
diff         = canonical transcript content after token N
```

Binary KV diffs are a later optimization. They need runtime layout support that the first CLI and gateway should not assume.

## User Prefill Capsules

User prefill capsules are root or early-parent checkpoints for stable context:

- user preferences
- project defaults
- repo maps
- tool rules
- app behavior

Because KV state is prefix-shaped, prefill capsules should be versioned rather than patched in the middle. A changed prefill source creates a new capsule version.

CLI shape:

```powershell
py -3 .\scripts\capsule_cli.py prefill create --endpoint local-llamacpp --name user_default --input .\user_prefill.md --soft
py -3 .\scripts\capsule_cli.py prefill create --endpoint local-llamacpp --name user_default --input .\user_prefill.md --hard --slot 0
py -3 .\scripts\capsule_cli.py prefill diff --name user_default --input .\user_prefill.md
py -3 .\scripts\capsule_cli.py thread start --endpoint local-llamacpp --prefill user_default --name new-thread
```

When a thread starts from a prefill capsule, the first live transcript message starts after the prefill token range. Later checkpoints preserve the prefill as a parent segment so the checkpoint still covers a contiguous context range from token zero.

## Request-Path Integration

The non-terminal integration path should be a local gateway, not a passive watcher:

```text
Open WebUI or opencode
  -> local capsule gateway
    -> model endpoint
```

The gateway can expose an OpenAI-compatible API while handling restore, append, checkpoint, and fallback behind the scenes.

Passive watchers may help diagnostics, but they cannot reliably know the exact prompt, tokenizer state, thread id, slot state, or restore result. The capsule layer belongs in the request path.

The local gateway prototype is:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode soft
```

Hard local mode uses a runtime slot:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode hard --slot 0
```

Clients should target `http://127.0.0.1:8765/v1/chat/completions` and send `stream=false`. The v0 gateway supports non-streaming chat completions only. It accepts:

- `X-Capsule-Thread`
- `X-Capsule-Workspace`
- `X-Capsule-Prefill`

For thin client integrations, the gateway also recognizes common forwarded identity headers such as `X-OpenWebUI-Chat-Id`, `X-OpenWebUI-User-Id`, `X-Opencode-Thread`, and `X-Opencode-Session`. Explicit `X-Capsule-*` headers remain preferred because they are client-independent.

The status endpoint advertises the identity contract:

```text
GET /api/capsules/status -> identity
```

For v0, the smallest useful thread metadata is one stable thread header:

- generic clients: `X-Capsule-Thread`
- Open WebUI: `X-OpenWebUI-Chat-Id`
- opencode: `X-Opencode-Thread` or `X-Opencode-Session`

Workspace headers are optional metadata. If no stable thread header exists, generated ids are best-effort and should not be treated as durable continuity.

If a hard capsule is available, the gateway restores it into the configured slot and forwards only the transcript diff after the capsule token range. If no compatible hard capsule exists, it forwards a replay prompt and checkpoints after the response.

The same gateway exposes `.scap` bundle transport for local UI and Model Plane integration:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
DELETE /api/capsules/bundles/{bundle_id}
```

The status response includes a versioned `transport` object so Model Plane can discover the running gateway's upload/download contract before issuing bundle operations.

Bundle transport moves portable thread artifacts. It still does not transport model weights, and hard snapshot blobs remain optional same-runtime artifacts.

## Model Plane Job Packets

Model Plane coordination should use the same ledger, manifest, and endpoint records as the standalone harness.

```text
Model Plane
  -> job packet
    -> capsule_cli.py job run
      -> existing checkpoint/resume/export/validate path
```

The first job-packet schema is:

```text
schemas/model-plane-job.schema.json
```

Supported job types:

- `resume_thread`
- `checkpoint_thread`
- `shutdown_thread`
- `export_thread`
- `validate_capsule`
- `gateway_export_bundle`
- `gateway_list_bundles`
- `gateway_download_bundle`
- `gateway_import_bundle`
- `gateway_delete_bundle`

Model Plane emits intent and policy. The capsule harness still owns runtime-specific restore, save, shutdown checkpointing, diff append, bundle export, gateway upload/download calls, and compatibility checks. Signing keys and gateway auth tokens stay outside packets and are supplied to the runner when needed.

## Storage Modes

Initial storage modes:

- `soft`: metadata checkpoint only, transcript replay required
- `server_side_handle`: backend retains state and the client carries an id
- `local_file`: local snapshot file controlled by the harness
- `scap_bundle`: exportable bundle with ledger, transcript, manifests, and optional snapshots

## `.scap` Bundles

The `.scap` bundle is a zip archive with a conservative default layout:

```text
manifest.json
thread-ledger.json
transcript.jsonl
capsule-index.json
endpoints/
prefills/
threads/
```

Default export is ledger-only. Hard snapshot blobs are omitted unless the user passes `--include-snapshots`.

```powershell
py -3 .\scripts\capsule_cli.py export --thread THREAD --out THREAD.scap
py -3 .\scripts\capsule_cli.py export --thread THREAD --out THREAD.scap --include-snapshots
py -3 .\scripts\capsule_cli.py import THREAD.scap
```

The bundle still does not contain model weights. If snapshots are omitted, a hard capsule may import with a manifest that points to a missing local snapshot; the safe fallback remains transcript replay.

## Compatibility

A hard capsule should be rejected or treated as soft-only if any of these mismatch:

- endpoint id
- model hash
- tokenizer hash
- runtime build or compatible build range
- context limit
- slot format

The safe fallback is always transcript replay.
