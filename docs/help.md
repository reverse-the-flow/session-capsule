# Help

## Mental Model

Session Capsules separate canonical conversation state from runtime acceleration state.

```text
endpoint   = where the model server lives
thread     = canonical transcript plus capsule ledger
capsule    = checkpoint manifest, optionally with a hard snapshot
prefill    = reusable root context
gateway    = local OpenAI-compatible request-path layer
integration = thin client config for Open WebUI, opencode, and local UIs
transport  = gateway API for .scap upload/download
security   = bundle integrity now, signing/encryption later
config     = persistent policy for capsule state
```

The transcript is the source of truth. Hard capsule blobs are cache unless pinned or exported.

## First Commands

Initialize persistent config:

```powershell
py -3 .\scripts\capsule_cli.py config init
```

Add an endpoint:

```powershell
py -3 .\scripts\capsule_cli.py endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080
```

Probe hard capsule support:

```powershell
py -3 .\scripts\capsule_cli.py endpoint doctor local-llamacpp --strict
```

Doctor records `/slots` evidence in the endpoint record: response shape, sample keys, candidate slot identity fields, configured chat slot field, and visible `n_ctx` / `is_processing` fields.

Summarize probed endpoint compatibility:

```powershell
py -3 .\scripts\capsule_cli.py endpoint matrix
py -3 .\scripts\capsule_cli.py endpoint matrix --json
```

The JSON form is the launcher-facing compatibility report for hard capsule controls.

Start a thread:

```powershell
py -3 .\scripts\capsule_cli.py thread start --endpoint local-llamacpp --name research-loop
```

Inspect state:

```powershell
py -3 .\scripts\capsule_cli.py inspect
```

Use the terminal help:

```powershell
py -3 .\scripts\capsule_cli.py help
py -3 .\scripts\capsule_cli.py help --topics
py -3 .\scripts\capsule_cli.py help storage
```

## Help Topics

The CLI help topics are:

- `overview`
- `config`
- `endpoint`
- `thread`
- `prefill`
- `gateway`
- `integrations`
- `transport`
- `storage`
- `state`
- `bundles`
- `security`
- `model-plane`
- `troubleshooting`

## Persistent Config

Persistent config belongs in:

```text
.capsules/config/settings.json
```

Use it for policy:

- `storage.max_bytes`
- `storage.min_free_bytes`
- `storage.prune_policy`
- `storage.keep_latest_per_thread`
- `storage.protect_active_prefills`

Storage budget is persistent because it is a lifecycle rule, not a one-process choice.

## State Location

V0 capsule state is project-local by default:

```text
.capsules/
```

Inspect the active state root:

```powershell
py -3 .\scripts\capsule_cli.py state info
```

Use `--state-dir` only when intentionally overriding the state root for tests, shared workspaces, or Model Plane launch profiles. User-level/global state is a future integration option, not the default.

## Launch Flags

Launch flags should describe the current process:

- `--state-dir`
- `--host`
- `--port`
- `--endpoint`
- `--checkpoint-mode`
- `--slot`
- `--default-prefill`
- `--timeout`
- `--max-bundle-bytes`
- `--auth-token-file`
- `--auth-token-env`
- `--cors-allow-origin`

These are good Model Plane launch-profile fields.

## Integrations

Generate an opencode provider config with stable capsule headers:

```powershell
py -3 .\scripts\capsule_cli.py integration opencode-config --workspace . --session default --prefill user_default --out .\.capsules\integrations\opencode.generated.json
```

The generated config writes `X-Capsule-Thread`, `X-Capsule-Workspace`, and `X-Capsule-Prefill` directly. The gateway token remains an environment reference, `{env:CAPSULE_GATEWAY_TOKEN}`, so secrets are not written into the config.

Open WebUI should point at the gateway's OpenAI-compatible base URL and forward chat/user headers when available.

## Storage Safety

GC deletes only eligible hard snapshot blobs.

It does not delete:

- transcripts
- thread ledgers
- manifests
- soft checkpoints
- pinned thread capsules

If GC deletes a hard snapshot blob, the ledger link is marked `missing` and transcript replay remains the fallback.
If hard restore fails, `resume --append-diff` marks that capsule `restore_failed`, replays the canonical transcript, and saves a replacement checkpoint.

## Gateway

The gateway runs beside the model endpoint:

```text
client
  -> capsule gateway
    -> model server
```

Run soft mode:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode soft
```

Run hard local mode:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode hard --slot 0
```

Client base URL:

```text
http://127.0.0.1:8765/v1
```

Gateway v0 is non-streaming. Clients should send `stream=false`.

Preferred identity headers:

- `X-Capsule-Thread`
- `X-Capsule-Workspace`
- `X-Capsule-Prefill`

Client-native thread headers recognized:

- `X-OpenWebUI-Chat-Id`
- `X-Opencode-Thread`
- `X-Opencode-Session`

Discover the full identity contract from `/api/capsules/status`.

## Transport

Gateway transport exposes `.scap` bundles to local UIs or Model Plane:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles
POST   /api/capsules/bundles
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
DELETE /api/capsules/bundles/{bundle_id}
```

Model Plane should read `/api/capsules/status` first. The response includes a versioned `transport` object with endpoint paths, `max_upload_bytes`, content type, auth policy, signing policy, and advertised upload/download capabilities. Launch profiles can list `transport.required_capabilities`; `gateway check` verifies every listed capability before Model Plane enables profile-dependent controls.

Browser-hosted Model Plane UIs should launch the gateway with `--cors-allow-origin` set to the exact UI origin and require `transport.cors.enabled` before enabling direct browser upload/download controls.

Bundles live under:

```text
.capsules/bundles/
```

Export is ledger-only by default. Hard snapshots require explicit opt-in with `include_snapshots=true`.

Raw uploads are capped by the gateway launch flag:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --max-bundle-bytes 5GB
```

Browser preflight is also a launch flag:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --cors-allow-origin http://127.0.0.1:3000
```

Gateway bundle signing is also launch policy:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --signature-key-file .\capsule-signing.key --signature-key-id local --require-bundle-signature
```

Gateway request auth is optional but should be enabled before binding beyond local-only use:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --auth-token-file .\capsule-gateway-token
```

Authenticated requests may use either:

- `Authorization: Bearer TOKEN`
- `X-Capsule-Gateway-Key: TOKEN`

Raw upload imports may use `X-Capsule-Import-Thread` to import the bundle as a new local thread id.

Store-only upload uses `POST /api/capsules/bundles` and does not create thread state. Import is a separate explicit `POST /api/capsules/import`.

## Security

Exported `.scap` bundles include per-entry SHA-256 digests in `manifest.json`.

Verify a bundle:

```powershell
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap
```

Inspect bundle share/import posture:

```powershell
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.scap
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.scap --json
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop.scap --preset metadata-only
```

Inspection reports plaintext transcript or prefill source content, hard snapshot inclusion, redaction status, signing, encryption status, and whether trusted transport is required.

`bundle-policy` is the script-friendly gate. Preset `metadata-only` rejects plaintext content and snapshots, `signed-metadata-only` also requires a signature, and `sealed` requires an encrypted envelope.

Gateway-side import policy uses the same presets:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --bundle-policy-preset metadata-only
```

This rejects raw uploads and stored-bundle imports before extraction.

Sign and verify with an explicit local key file:

```powershell
py -3 .\scripts\capsule_cli.py export --thread research-loop --out .\research-loop.scap --signature-key-file .\capsule-signing.key --signature-key-id local
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap --signature-key-file .\capsule-signing.key --require-signature
```

Seal and unseal with an external age-compatible command:

```powershell
py -3 .\scripts\capsule_cli.py seal .\research-loop.scap --out .\research-loop.sealed.scap --age-recipient age1...
py -3 .\scripts\capsule_cli.py unseal .\research-loop.sealed.scap --out .\research-loop.unsealed.scap --age-identity .\age-identity.txt
```

Preview export size without writing:

```powershell
py -3 .\scripts\capsule_cli.py export --thread research-loop --out .\research-loop.scap --dry-run
```

Export a metadata-only bundle without transcript or prefill source text:

```powershell
py -3 .\scripts\capsule_cli.py export --thread research-loop --out .\research-loop-redacted.scap --redact-transcript
```

Import verifies bundles that include `file_digests`, rejects duplicate or digest-mismatched entries, and warns when an incoming endpoint record differs from an existing local endpoint with the same id. Use `import --thread-id NEW_ID` to import a bundle as a new local thread.
Redacted imports preserve `transcript_redacted=true` and mark transcript replay fallback unavailable.

Current boundary:

- implemented: digest-based integrity checks
- implemented: optional HMAC-SHA256 signatures
- implemented: external age-compatible sealed bundle envelopes
- implemented: metadata-only redacted transcript export
- key sources: `--signature-key-file` or `--signature-key-env`
- keys are not stored in `.capsules`
- gateway signing/required-signature import is controlled by launch flags
- Model Plane export jobs can sign with runner-side `job run --signature-key-file` or `job run --signature-key-env`
- gateway request auth is controlled by `--auth-token-file` or `--auth-token-env`
- gateway transport jobs authenticate with `job run --gateway-auth-token-file` or `job run --gateway-auth-token-env`
- bundle inspection classifies transported bundles for share/import policy before upload or import
- bundle policy checks fail with a nonzero exit code when a bundle does not meet the requested share/import requirements
- gateway import policy can enforce the same checks server-side before extraction
- not implemented yet: hosted/provider-side sealed capsules or user-carried runtime blobs

Redaction is not encryption. It removes transcript and prefill source text, but metadata, digests, token ranges, endpoint ids, and included hard snapshots may still be sensitive. Local sealing delegates encryption to an external backend rather than implementing crypto in this repo.

## Model Plane

Model Plane should supervise the gateway, not absorb it.

Model Plane owns launch profiles, lifecycle, health checks, and job routing. The gateway owns the request path and capsule restore/checkpoint behavior. The model runtime owns model weights, live KV cache, slots, and generation.

Gateway launch profile artifacts:

- `schemas/model-plane-gateway-launch.schema.json`
- `examples/model-plane/gateway-launch-profile.example.json`

The launch profile stores gateway wiring, required gateway transport capabilities, and secret references only. It does not store gateway tokens or signing key values.

Render a gateway launch command:

```powershell
py -3 .\scripts\capsule_cli.py gateway command .\examples\model-plane\gateway-launch-profile.example.json --json
py -3 .\scripts\capsule_cli.py gateway check .\examples\model-plane\gateway-launch-profile.example.json --json
```

For `gateway check`, relative file secret references are resolved from the profile directory.

`gateway check --json` reports:

- `transport_verified`
- `endpoint_verified`
- `endpoint_compatibility`
- `required_capabilities`
- `transport_capabilities`

Hard checkpoint profiles require a `slot_probe_ok` endpoint before `endpoint_verified` can pass.

Supported job packet types:

- `resume_thread`
- `checkpoint_thread`
- `shutdown_thread`
- `export_thread`
- `validate_capsule`
- `gateway_export_bundle`
- `gateway_list_bundles`
- `gateway_store_bundle`
- `gateway_download_bundle`
- `gateway_import_bundle`
- `gateway_delete_bundle`

Shutdown jobs let Model Plane ask the harness to save dirty thread state before unloading a runtime.

Signed export job packets keep signing keys outside the packet:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\export-thread.example.json --signature-key-file .\capsule-signing.key --signature-key-id local
```

Protected gateway job packets keep auth outside the packet:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
```
