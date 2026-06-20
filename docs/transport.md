# Capsule Transport

## Purpose

Capsule transport is the upload/download layer for `.scap` bundles.

It does not move model weights. It moves the portable thread artifact: ledger, transcript, endpoint metadata, capsule manifests, prefill sources, and optional same-runtime hard snapshots.

The gateway owns local bundle creation, local bundle storage, store-only upload, download, upload-and-import, and import. Model Plane or another UI can call these endpoints instead of reimplementing export/import mechanics.

Gateway bundle signing is launch policy. If the gateway is started with `--signature-key-file` or `--signature-key-env`, exported bundles are signed. If it is also started with `--require-bundle-signature`, imports must verify with that key before extraction.

Gateway bundle import policy is also launch policy. For example:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --bundle-policy-preset metadata-only
```

The gateway applies this policy to raw upload-imports and stored-bundle imports before extraction. Store-only uploads are verified as `.scap` archives before entering the bundle store, but import policy is applied only when a bundle is imported. The active policy is advertised from `/api/capsules/status` as `transport.import_policy`.

Gateway request auth is also launch policy. If the gateway is started with `--auth-token-file` or `--auth-token-env`, every request must include either:

```text
Authorization: Bearer TOKEN
X-Capsule-Gateway-Key: TOKEN
```

## Gateway Endpoints

The local gateway exposes:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles
POST   /api/capsules/bundles
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/handoff
POST   /api/capsules/import
DELETE /api/capsules/bundles/{bundle_id}
```

Bundles are stored under:

```text
.capsules/bundles/
```

## Discovery

Model Plane should check gateway status before enabling upload/download controls:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/capsules/status
```

The response includes a versioned `transport` object:

```json
{
  "transport": {
    "api_version": "0.1",
    "bundle_format": "session-capsules.scap",
    "bundle_content_type": "application/vnd.session-capsule.scap",
    "max_upload_bytes": 5368709120,
    "capabilities": {
      "export": true,
      "list": true,
      "download": true,
      "store_upload": true,
      "raw_upload_import": true,
      "stored_bundle_import": true,
      "handoff": true,
      "upload_handshake": true,
      "download_handshake": true,
      "delete": true,
      "thread_id_override": true,
      "digest_verification": true,
      "hmac_sha256_signing": true,
      "require_signature_on_import": false
    },
    "endpoints": {
      "export": {"method": "POST", "path": "/api/capsules/export"},
      "store_bundle": {"method": "POST", "path": "/api/capsules/bundles"},
      "download_bundle": {"method": "GET", "path_template": "/api/capsules/bundles/{bundle_id}"},
      "handoff": {"method": "POST", "path": "/api/capsules/handoff"},
      "import": {"method": "POST", "path": "/api/capsules/import"}
    },
    "auth": {
      "required": true,
      "accepted_headers": ["Authorization: Bearer TOKEN", "X-Capsule-Gateway-Key"]
    },
    "cors": {
      "enabled": true,
      "allow_origin": "http://127.0.0.1:3000",
      "preflight": true
    },
    "signing": {
      "exports_signed": true,
      "signature_key_id": "local",
      "required_on_import": false
    },
    "import_policy": {
      "preset": "metadata-only",
      "requirements": ["disallow_plaintext", "disallow_snapshots"]
    }
  }
}
```

The same response includes `endpoint_compatibility` so launchers can tell whether hard checkpoint controls are safe to expose for this gateway instance:

```json
{
  "endpoint_compatibility": {
    "endpoint_id": "local-llamacpp",
    "slot_save_restore": true,
    "slot_probe": {
      "status": "slot_probe_ok",
      "response_shape": "list",
      "slot_count": 1
    },
    "hard_checkpoint_required": true,
    "hard_checkpoint_ready": true
  }
}
```

The same response includes an `identity` object for thread continuity:

```json
{
  "identity": {
    "preferred_headers": {
      "thread": "X-Capsule-Thread",
      "workspace": "X-Capsule-Workspace",
      "prefill": "X-Capsule-Prefill"
    },
    "client_mappings": {
      "open_webui": {
        "minimum_thread_header": "X-OpenWebUI-Chat-Id",
        "optional_workspace_header": "X-OpenWebUI-User-Id"
      },
      "opencode": {
        "minimum_thread_headers": ["X-Opencode-Thread", "X-Opencode-Session"],
        "optional_workspace_header": "X-Opencode-Workspace"
      }
    }
  }
}
```

These are runtime contracts for launchers and local UIs. The docs describe the same API, but the status payload tells Model Plane what this gateway instance actually started with. A Model Plane launch profile can list `transport.required_capabilities`; `gateway check` verifies those names against `transport.capabilities` before upload/download controls are enabled.

## Handoff Handshake

Tray and UI clients should handshake with the gateway before moving capsule
bytes. The handoff endpoint does not send prompt traffic and does not touch
runtime slots. It only exchanges transfer facts and returns the exact next HTTP
operation.

Prepare an upload:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/handoff `
  -Body '{"operation":"upload","mode":"import","bundle_id":"research-loop","thread_id":"research-loop-copy","size_bytes":12345,"sha256":"SHA256_HEX"}'
```

Accepted upload handshakes include:

```json
{
  "operation": "upload",
  "phase": "prepare",
  "accepted": true,
  "handoff_id": "handoff-...",
  "upload": {
    "method": "POST",
    "path": "/api/capsules/import",
    "content_type": "application/vnd.session-capsule.scap",
    "headers": {
      "X-Capsule-Handoff-Id": "handoff-...",
      "X-Capsule-Bundle-Id": "research-loop",
      "X-Capsule-Bundle-SHA256": "sha256:..."
    }
  },
  "commit": {
    "method": "POST",
    "path": "/api/capsules/handoff",
    "content_type": "application/json"
  }
}
```

The tray uses the JSON `commit` form so the gateway can accept or reject before
the staged artifact leaves the tray. Browser or CLI clients can instead use the
returned binary `upload` target and include `X-Capsule-Handoff-Id`; the gateway
then checks the bundle id, mode, size, and SHA-256 before store/import.

Prepare a download:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/handoff `
  -Body '{"operation":"download","bundle_id":"research-loop"}'
```

Accepted download handshakes return the bundle metadata, SHA-256, and a `GET`
target under `/api/capsules/bundles/{bundle_id}`. If the client includes the
returned `X-Capsule-Handoff-Id` on the download request, the gateway validates
that the handoff id matches the bundle and echoes it in the response headers.

The handoff id is short-lived transfer evidence, not a substitute for gateway
auth. If the gateway was launched with request auth, every handoff and transfer
request still needs the configured bearer token or `X-Capsule-Gateway-Key`.

## Browser Access

Browser-hosted Model Plane UIs need CORS preflight before they can call the local gateway directly for `.scap` upload/download.

Enable one exact UI origin at gateway launch:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --cors-allow-origin http://127.0.0.1:3000
```

The gateway then answers `OPTIONS` preflight for upload/download/control requests and exposes capsule download headers such as `X-Capsule-Bundle-Id` and `X-Capsule-Bundle-SHA256` to browser code.

Use `*` only for local development where another auth and exposure boundary already exists. For normal Model Plane use, configure the exact origin and keep request auth enabled if the gateway is reachable beyond the local host.

## Export

Create a bundle from a local thread:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/export `
  -Body '{"thread_id":"research-loop","include_snapshots":false,"redact_transcript":false}'
```

Response shape:

```json
{
  "bundle_id": "research-loop-20260618-112233-a1b2c3d4",
  "filename": "research-loop-20260618-112233-a1b2c3d4.scap",
  "thread_id": "research-loop",
  "download_url": "/api/capsules/bundles/research-loop-20260618-112233-a1b2c3d4",
  "size_bytes": 12345,
  "sha256": "sha256:...",
  "export_mode": "ledger-only",
  "includes_snapshots": false,
  "redacted_transcript": false
}
```

Export defaults to ledger-only. Set `include_snapshots=true` only when intentionally moving same-runtime hard snapshot blobs.

Every new export includes `file_digests` in `manifest.json`. The digest index covers every zip entry except `manifest.json`.

Set `redact_transcript=true` to produce a metadata-only bundle. Redacted exports:

- write empty transcript files
- omit prefill source text files
- mark thread ledgers with `transcript_redacted=true`
- set fallback mode to `unavailable_redacted_transcript`
- mark prefill manifests with `prefill_source.source_redacted=true`

Redaction is not encryption. It removes transcript and prefill source text from the bundle, but remaining metadata, digests, token ranges, endpoint ids, and included hard snapshots may still be sensitive.

If the gateway has a configured signing key, the export response metadata includes:

```json
{
  "signature_present": true,
  "signature_algorithm": "hmac-sha256",
  "signature_key_id": "local"
}
```

## List And Download

List local bundles:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/capsules/bundles
```

Download a bundle:

```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/api/capsules/bundles/research-loop-20260618-112233-a1b2c3d4 `
  -OutFile .\research-loop.scap
```

Download responses use:

```text
Content-Type: application/vnd.session-capsule.scap
Content-Disposition: attachment; filename="BUNDLE.scap"
X-Capsule-Bundle-Id: BUNDLE
X-Capsule-Bundle-SHA256: sha256:...
```

## Direct CLI Client

The standalone CLI can call the same gateway upload/download endpoints without a Model Plane job packet. This is the simplest manual integration path and a useful smoke test for launch profiles:

```powershell
py -3 .\scripts\capsule_cli.py gateway status --url http://127.0.0.1:8765 --auth-token-file .\capsule-gateway-token --json
py -3 .\scripts\capsule_cli.py gateway list --url http://127.0.0.1:8765 --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway export --url http://127.0.0.1:8765 --thread research-loop --bundle-id research-loop --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway download --url http://127.0.0.1:8765 --bundle-id research-loop --out .\research-loop.scap --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\research-loop.scap --bundle-id stored-research-loop --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway upload --url http://127.0.0.1:8765 --bundle .\research-loop.scap --bundle-id uploaded-research-loop --thread-id research-loop-copy --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway import --url http://127.0.0.1:8765 --bundle-id uploaded-research-loop --thread-id research-loop-copy-2 --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway delete --url http://127.0.0.1:8765 --bundle-id uploaded-research-loop --auth-token-file .\capsule-gateway-token
```

Use `gateway store` when the user only wants to place a bundle in the gateway bundle store for later download or explicit import. Use `gateway upload` when the operation should both upload and import. Add `--policy-preset metadata-only`, `signed-metadata-only`, or `sealed` to `gateway store` or `gateway upload` when the local command should fail before disallowed `.scap` bytes are sent.

These commands are thin wrappers around the gateway API. They do not store auth tokens in `.capsules`, job packets, or bundle metadata.

## Store And Import

Store raw `.scap` bytes without importing them:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/vnd.session-capsule.scap" `
  -Headers @{"X-Capsule-Bundle-Id" = "stored-research-loop"} `
  -InFile .\research-loop.scap `
  -Uri http://127.0.0.1:8765/api/capsules/bundles
```

Use `X-Capsule-Bundle-Force: true` only when intentionally replacing an existing stored bundle. Store-only upload verifies the digest-indexed `.scap` envelope and does not create thread state.

Import a bundle already stored under `.capsules/bundles/`:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/import `
  -Body '{"bundle_id":"research-loop-20260618-112233-a1b2c3d4","force":false}'
```

Import a stored bundle under a new local thread id:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/import `
  -Body '{"bundle_id":"research-loop-20260618-112233-a1b2c3d4","thread_id":"research-loop-copy","force":false}'
```

Upload and import raw `.scap` bytes in one step:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/vnd.session-capsule.scap" `
  -Headers @{"X-Capsule-Bundle-Id" = "uploaded-research-loop"} `
  -InFile .\research-loop.scap `
  -Uri http://127.0.0.1:8765/api/capsules/import
```

Use `X-Capsule-Import-Thread: research-loop-copy` to import raw uploaded bytes under a new local thread id.

Use `X-Capsule-Import-Force: true` only when intentionally replacing an existing local thread.

Imports verify bundles that include `file_digests`. Digest mismatch or duplicate zip entries fail before extraction. When a target thread id is supplied, thread-owned ledger, transcript, manifest, and snapshot refs are remapped under `threads/TARGET/`; endpoint and prefill records remain shared state records. Redacted imports warn that transcript content is unavailable and preserve the unavailable replay fallback.

## Verify

Verify a bundle before upload or import:

```powershell
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap
```

Inspect share/import posture before exposing a bundle:

```powershell
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.scap
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.scap --json
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop.scap --preset metadata-only
```

Inspection reports whether transcript or prefill source text is present in plaintext, whether hard snapshots are included, whether the bundle is redacted, signed, or encrypted, and whether trusted transport is required. Gateway bundle listings expose the same classification as `share_safety`.

`bundle-policy` is the exit-code gate for launchers and scripts. Preset `metadata-only` rejects plaintext transcript/prefill source content and snapshots; `signed-metadata-only` also requires a signature; `sealed` requires an encrypted envelope.

Verify a signed bundle with an explicit key:

```powershell
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap --signature-key-file .\capsule-signing.key --require-signature
```

The digest index proves archive integrity for the exported files. HMAC signing proves possession of the shared signing key.

Seal a bundle with an external age-compatible command:

```powershell
py -3 .\scripts\capsule_cli.py seal .\research-loop.scap --out .\research-loop.sealed.scap --age-recipient-file .\.capsules\security\recipients\local.agepub
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop.sealed.scap --preset sealed
py -3 .\scripts\capsule_cli.py unseal .\research-loop.sealed.scap --out .\research-loop.unsealed.scap --age-identity C:\Users\you\.config\age\keys.txt
```

The sealed file is a small inspectable envelope containing `manifest.json` plus an externally encrypted payload. Import requires an explicit `unseal` step first. See [docs/sealing.md](/X:/Experiments/session-capsules/docs/sealing.md) for the recommended age-compatible backend and key-reference policy.

## Delete

Delete a stored local bundle after transfer:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri http://127.0.0.1:8765/api/capsules/bundles/research-loop-20260618-112233-a1b2c3d4
```

Deleting a bundle does not delete imported thread state, transcripts, ledgers, manifests, or hard snapshots.

## Model Plane Boundary

Model Plane should treat these endpoints as a local control-plane primitive.

Gateway owns:

- bundle export from local state
- bundle list/download/delete under `.capsules/bundles/`
- raw `.scap` upload
- import into local capsule state
- max upload size enforcement
- optional signing and required-signature verification as launch policy
- optional request-token authentication as launch policy
- optional browser CORS preflight as launch policy

Model Plane owns:

- user-facing upload/download UI
- authentication and remote exposure
- retention policy and cleanup schedule
- audit/event history
- deciding when a bundle should be exported, imported, pinned, or deleted

This keeps the transport layer standalone while making Model Plane integration practical.

## Security Boundary

Implemented now:

- per-entry SHA-256 file digests in bundle `manifest.json`
- optional HMAC-SHA256 signatures in bundle `manifest.json`
- sealed bundle envelopes using an external age-compatible command
- metadata-only redacted transcript export
- `capsule verify BUNDLE.scap`
- import-time digest verification for bundles that carry `file_digests`
- duplicate zip-entry rejection
- required signature verification with `--require-signature`
- gateway signing and required import verification through launch flags
- gateway request-token authentication through launch flags
- gateway store-only upload separate from import

Not implemented yet:

- hosted/provider-side sealed capsules
- user-carried runtime blobs

The local sealed envelope builds on the digest and signature envelope instead of replacing it. The repo delegates encryption to an external command and does not implement its own cryptographic primitive.

## Model Plane Job Packets

The standalone harness can call the gateway transport API from Model Plane job packets:

```text
gateway_export_bundle
gateway_list_bundles
gateway_store_bundle
gateway_download_bundle
gateway_import_bundle
gateway_delete_bundle
```

Examples live under:

```text
examples/model-plane/gateway-*.example.json
```

These job packets carry intent and policy inputs. They do not replace the gateway API; they call it.

For `gateway_store_bundle`, `params.bundle` is stored without creating thread state. Store jobs may include `policy_preset` or the explicit bundle-policy booleans to fail locally before sending bytes. For `gateway_import_bundle`, `params.thread_id` is the target local thread id for the imported bundle.

If the gateway requires auth, keep the token outside the packet and pass it to the standalone runner:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
```
