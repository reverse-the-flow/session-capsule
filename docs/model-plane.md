# Model Plane Coordination

## Boundary

Model Plane should coordinate capsule-capable work. It should not become the inference backend.

Model Plane can own:

- endpoint registry
- endpoint capability cache
- thread and capsule registry
- job packets
- routing and fallback policy
- user-facing bundle upload/download policy

Model Plane should not own:

- model weights
- live KV tensors
- runtime slot layout
- local snapshot files except as artifact references
- the primary generation loop

## Job Packets

The Stage 8 contract is a small JSON job packet consumed by the standalone harness:

```text
schemas/model-plane-job.schema.json
examples/model-plane/*.example.json
```

Supported job types:

- `resume_thread`
- `checkpoint_thread`
- `export_thread`
- `validate_capsule`
- `gateway_export_bundle`
- `gateway_list_bundles`
- `gateway_download_bundle`
- `gateway_import_bundle`
- `gateway_delete_bundle`

Run a packet with:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\checkpoint-thread.example.json
```

Inspect without executing:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\checkpoint-thread.example.json --dry-run
```

## Execution Model

Model Plane emits intent. The capsule harness executes intent.

```text
Model Plane job packet
  -> capsule_cli.py job run
    -> thread ledger
    -> endpoint capability record
    -> runtime adapter if a hard restore/save is needed
```

This keeps the standalone CLI and gateway useful without Model Plane. It also lets Model Plane schedule capsule-aware work later without learning runtime-specific slot APIs.

For UI-driven `.scap` transfer, Model Plane should call the gateway bundle endpoints instead of reimplementing the archive format:

```text
POST   /api/capsules/export
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
```

The gateway owns local export/import mechanics. Model Plane owns auth, UX, retention, and remote exposure.

If signed transport is required, Model Plane should launch the gateway with `--signature-key-file` or `--signature-key-env` and `--require-bundle-signature`. Signing keys should not be placed inside job packets.

For direct `export_thread` packets, the standalone runner can sign the output bundle with `--signature-key-file` or `--signature-key-env`. The packet carries the export intent; the runner carries the secret.

If gateway auth is required, Model Plane should launch the gateway with `--auth-token-file` or `--auth-token-env` and provide the token to clients as a bearer API key or `X-Capsule-Gateway-Key`. Auth tokens should not be placed inside job packets. For standalone job execution, pass the token to the runner with `--gateway-auth-token-file` or `--gateway-auth-token-env`.

The standalone harness can execute those transport intents as job packets:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-export-bundle.example.json
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-import-bundle.example.json
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\export-thread.example.json --signature-key-file .\capsule-signing.key --signature-key-id local
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
```

## Fallback

Every job must be safe to degrade:

- `resume_thread` falls back only through the existing CLI restore/replay behavior.
- `checkpoint_thread` can use `mode=soft` when no runtime slot is available.
- `export_thread` omits hard snapshots unless explicitly requested.
- `validate_capsule` can report a missing local snapshot without invalidating the canonical transcript.
- Gateway transport jobs fail as transfer/control-plane operations without invalidating the canonical transcript.

The transcript remains the source of truth. Capsules remain acceleration artifacts.
