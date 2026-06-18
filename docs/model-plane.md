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
- `shutdown_thread`
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
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\shutdown-thread.example.json --dry-run
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

## Gateway Launch Profiles

Model Plane can launch the gateway from a small profile instead of hardcoding CLI flags:

```text
schemas/model-plane-gateway-launch.schema.json
examples/model-plane/gateway-launch-profile.example.json
```

The launch profile describes:

- gateway state directory, endpoint id, host, port, checkpoint mode, slot, timeout, and max bundle upload size
- OpenAI-compatible base URL and status URL
- optional browser origin allowed to call gateway upload/download endpoints
- request-auth and bundle-signing secret references
- bundle import policy for server-side upload/import rejection
- whether import requires signed bundles

The profile must contain only secret references, not secret values. For example, it may point at `.capsule-gateway-token` or `CAPSULE_GATEWAY_TOKEN`, but it must not contain the token itself.

Render the gateway command from a profile:

```powershell
py -3 .\scripts\capsule_cli.py gateway command .\examples\model-plane\gateway-launch-profile.example.json --json
```

After Model Plane launches the gateway, it should check the running process against the profile:

```powershell
py -3 .\scripts\capsule_cli.py gateway check .\examples\model-plane\gateway-launch-profile.example.json --json
```

That check calls the profile's `transport.status_url`, authenticates from `security.request_auth`, and verifies that the status response matches the profile and includes the required `transport` object before upload/download controls are enabled.

For `gateway check`, relative file secret references are resolved from the profile directory. The profile still stores only references, not token or key values.

For UI-driven `.scap` transfer, Model Plane should call the gateway bundle endpoints instead of reimplementing the archive format:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
```

Model Plane should read `/api/capsules/status` first and use its `transport` object as the runtime contract. It advertises the API version, max raw upload bytes, `.scap` content type, endpoint paths, auth requirement, signing policy, import policy, and upload/download capabilities for the specific gateway instance that was launched.

If Model Plane's upload/download controls run in a browser, the launch profile should set `gateway.cors_allow_origin` to that UI's exact origin. The status response then advertises `transport.cors`; Model Plane should require it before enabling direct browser `.scap` transfer controls.

The same status response includes `identity`, which advertises preferred `X-Capsule-*` headers plus recognized Open WebUI and opencode thread headers. Model Plane should use that object when deciding which UI/session id to bind to `X-Capsule-Thread`.

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

For `gateway_import_bundle`, `params.thread_id` is the target local thread id to import as. This lets Model Plane avoid clobbering an existing local thread when a user uploads or reimports a `.scap` bundle.

Before enabling external share/import affordances, Model Plane should inspect the bundle posture. For local files, call:

```powershell
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop.scap --json
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop.scap --preset metadata-only --json
```

For gateway-stored bundles, `GET /api/capsules/bundles` exposes the same classification as `share_safety` plus `trusted_transport_required`, plaintext-content flags, snapshot inclusion, signing, and encryption metadata. `metadata_only_not_encrypted` means transcript and prefill source text were omitted, but the bundle is still not sealed. `contains_plaintext_content` and `contains_unencrypted_snapshots` should stay behind trusted transport unless a later encryption envelope is present.

For direct CLI-driven uploads, pass `--policy-preset metadata-only`, `--policy-preset signed-metadata-only`, or `--policy-preset sealed` to `gateway upload` to fail locally before sending bytes. The `sealed` preset is intentionally forward-looking: it will fail until an encryption envelope exists.

For gateway-driven uploads, put the same intent in `security.bundle_import_policy`. `gateway command` renders it into `--bundle-policy-*` flags, and `gateway check` verifies the running gateway advertises the same `transport.import_policy`.

## Fallback

Every job must be safe to degrade:

- `resume_thread` falls back only through the existing CLI restore/replay behavior.
- `checkpoint_thread` can use `mode=soft` when no runtime slot is available.
- `shutdown_thread` saves a dirty checkpoint before Model Plane unloads the runtime.
- `export_thread` omits hard snapshots unless explicitly requested.
- `validate_capsule` can report a missing local snapshot without invalidating the canonical transcript.
- Gateway transport jobs fail as transfer/control-plane operations without invalidating the canonical transcript.

The transcript remains the source of truth. Capsules remain acceleration artifacts.
