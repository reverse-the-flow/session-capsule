# Capsule Transport

## Purpose

Capsule transport is the upload/download layer for `.scap` bundles.

It does not move model weights. It moves the portable thread artifact: ledger, transcript, endpoint metadata, capsule manifests, prefill sources, and optional same-runtime hard snapshots.

The gateway owns local bundle creation, local bundle storage, download, upload, and import. Model Plane or another UI can call these endpoints instead of reimplementing export/import mechanics.

## Gateway Endpoints

The local gateway exposes:

```text
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

Model Plane owns:

- user-facing upload/download UI
- authentication and remote exposure
- retention policy and cleanup schedule
- audit/event history
- deciding when a bundle should be exported, imported, pinned, or deleted

This keeps the transport layer standalone while making Model Plane integration practical.
