# Capsule Transport

## Purpose

Capsule transport is the upload/download layer for `.scap` bundles.

It does not move model weights. It moves the portable thread artifact: ledger, transcript, endpoint metadata, capsule manifests, prefill sources, and optional same-runtime hard snapshots.

The gateway owns local bundle creation, local bundle storage, download, upload, and import. Model Plane or another UI can call these endpoints instead of reimplementing export/import mechanics.

Gateway bundle signing is launch policy. If the gateway is started with `--signature-key-file` or `--signature-key-env`, exported bundles are signed. If it is also started with `--require-bundle-signature`, imports must verify with that key before extraction.

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
GET    /api/capsules/bundles/{bundle_id}
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
      "raw_upload_import": true,
      "stored_bundle_import": true,
      "delete": true,
      "digest_verification": true,
      "hmac_sha256_signing": true,
      "require_signature_on_import": false
    },
    "endpoints": {
      "export": {"method": "POST", "path": "/api/capsules/export"},
      "download_bundle": {"method": "GET", "path_template": "/api/capsules/bundles/{bundle_id}"},
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
    }
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

These are runtime contracts for launchers and local UIs. The docs describe the same API, but the status payload tells Model Plane what this gateway instance actually started with.

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

## Import

Import a bundle already stored under `.capsules/bundles/`:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/import `
  -Body '{"bundle_id":"research-loop-20260618-112233-a1b2c3d4","force":false}'
```

Upload and import raw `.scap` bytes:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/vnd.session-capsule.scap" `
  -Headers @{"X-Capsule-Bundle-Id" = "uploaded-research-loop"} `
  -InFile .\research-loop.scap `
  -Uri http://127.0.0.1:8765/api/capsules/import
```

Use `X-Capsule-Import-Force: true` only when intentionally replacing an existing local thread.

Imports verify bundles that include `file_digests`. Digest mismatch or duplicate zip entries fail before extraction.

## Verify

Verify a bundle before upload or import:

```powershell
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap
```

Verify a signed bundle with an explicit key:

```powershell
py -3 .\scripts\capsule_cli.py verify .\research-loop.scap --signature-key-file .\capsule-signing.key --require-signature
```

The digest index proves archive integrity for the exported files. HMAC signing proves possession of the shared signing key.

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
- `capsule verify BUNDLE.scap`
- import-time digest verification for bundles that carry `file_digests`
- duplicate zip-entry rejection
- required signature verification with `--require-signature`
- gateway signing and required import verification through launch flags
- gateway request-token authentication through launch flags

Not implemented yet:

- encryption
- sealed user-carried blobs

Encryption and sealed-blob features should build on the digest and signature envelope instead of replacing it.

## Model Plane Job Packets

The standalone harness can call the gateway transport API from Model Plane job packets:

```text
gateway_export_bundle
gateway_list_bundles
gateway_download_bundle
gateway_import_bundle
gateway_delete_bundle
```

Examples live under:

```text
examples/model-plane/gateway-*.example.json
```

These job packets carry intent and policy inputs. They do not replace the gateway API; they call it.

If the gateway requires auth, keep the token outside the packet and pass it to the standalone runner:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
```
