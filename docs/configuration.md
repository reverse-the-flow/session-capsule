# Configuration

## Where Settings Live

Session Capsules use three configuration layers:

1. Persistent capsule state config
2. Launch flags for one process
3. Endpoint and thread records created by the CLI

The persistent config lives in:

```text
.capsules/config/settings.json
```

Use it for policy that should survive restarts, especially storage lifecycle rules.

Launch flags are still the right place for process-specific values such as gateway port, gateway host, runtime slot, and one-off timeouts.

## Persistent Settings

Create the default config:

```powershell
py -3 .\scripts\capsule_cli.py config init
```

Show all settings:

```powershell
py -3 .\scripts\capsule_cli.py config show
```

Set the storage budget:

```powershell
py -3 .\scripts\capsule_cli.py config set storage.max_bytes 50GB
py -3 .\scripts\capsule_cli.py config set storage.min_free_bytes 20GB
```

Initial persistent settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `storage.max_bytes` | `50GB` | Soft quota for hard capsule snapshot blobs. |
| `storage.min_free_bytes` | `20GB` | Prune when disk free space drops below this floor. |
| `storage.prune_policy` | `oldest_unpinned_first` | Delete oldest eligible hard snapshot blobs first. |
| `storage.keep_latest_per_thread` | `1` | Protect this many newest hard snapshots per thread. |
| `storage.protect_active_prefills` | `true` | Active hard prefill snapshots are protected. |

## Storage Commands

Inspect storage:

```powershell
py -3 .\scripts\capsule_cli.py stats
```

Preview cleanup:

```powershell
py -3 .\scripts\capsule_cli.py gc --dry-run
```

Apply cleanup:

```powershell
py -3 .\scripts\capsule_cli.py gc --apply
```

One-run budget override:

```powershell
py -3 .\scripts\capsule_cli.py gc --dry-run --max-bytes 10GB --min-free-bytes 30GB
```

Pin the active capsule for a thread:

```powershell
py -3 .\scripts\capsule_cli.py pin --thread THREAD
```

Pin a specific capsule:

```powershell
py -3 .\scripts\capsule_cli.py pin --thread THREAD --capsule-id CAP_ID
```

Unpin:

```powershell
py -3 .\scripts\capsule_cli.py unpin --thread THREAD --capsule-id CAP_ID
```

GC never deletes transcripts, ledgers, or manifests. It deletes only eligible hard snapshot blobs and marks the ledger link as `missing`, preserving transcript replay fallback.

Pinned thread capsules are always protected. That is an invariant, not a setting.

## Launch Flags

These should stay as launch-time values because they describe the current process, not durable policy:

| Setting | Example | Why |
| --- | --- | --- |
| `--state-dir` | `.capsules` | Allows separate projects or test states. |
| `--host` | `127.0.0.1` | Gateway bind address. |
| `--port` | `8765` | Gateway listen port. |
| `--endpoint` | `local-llamacpp` | Gateway target endpoint for this launch. |
| `--checkpoint-mode` | `soft`, `hard`, `none` | Runtime behavior for this gateway process. |
| `--slot` | `0` | Runtime slot used by this hard-mode process. |
| `--default-prefill` | `user_default` | Optional launch default. |
| `--timeout` | `120` | Network/runtime timeout for this launch. |
| `--max-bundle-bytes` | `5GB` | Maximum raw `.scap` upload accepted by this gateway process. |
| `--signature-key-file` | `.capsule-signing.key` | Optional gateway signing key file for bundle export/import verification. |
| `--signature-key-env` | `CAPSULE_SIGNING_KEY` | Optional gateway signing key environment variable. |
| `--signature-key-id` | `local` | Non-secret label written into signed bundles. |
| `--require-bundle-signature` | flag | Requires gateway imports to verify with the configured signing key. |
| `--auth-token-file` | `.capsule-gateway-token` | Optional gateway request token file. |
| `--auth-token-env` | `CAPSULE_GATEWAY_TOKEN` | Optional gateway request token environment variable. |

## Secret Inputs

Signature keys are secret inputs, not persistent settings:

| Setting | Example | Why |
| --- | --- | --- |
| `--signature-key-file` | `.capsule-signing.key` | Reads a local signing key for export/import/verify without storing it in `.capsules`. |
| `--signature-key-env` | `CAPSULE_SIGNING_KEY` | Reads a signing key from the process environment. |
| `--signature-key-id` | `local` | Non-secret label written into signed bundle metadata. |
| `job run --signature-key-file` | `.capsule-signing.key` | Lets the standalone job runner sign an `export_thread` bundle without placing the key in the packet. |
| `job run --signature-key-env` | `CAPSULE_SIGNING_KEY` | Reads the export job signing key from the process environment. |
| `--auth-token-file` | `.capsule-gateway-token` | Reads a request token required by the gateway. |
| `--auth-token-env` | `CAPSULE_GATEWAY_TOKEN` | Reads a request token from the process environment. |
| `job run --gateway-auth-token-file` | `.capsule-gateway-token` | Lets the standalone job runner call a protected gateway without placing the token in the packet. |
| `job run --gateway-auth-token-env` | `CAPSULE_GATEWAY_TOKEN` | Reads the protected gateway job-runner token from the process environment. |

For the gateway and Model Plane job runner, these are launch-profile or command-runner values. Do not put signing keys or gateway auth tokens in `settings.json`, endpoint records, or Model Plane job packets.

## Model Plane Launch Profile

Model Plane can keep gateway launch wiring in a profile instead of treating the gateway as an opaque shell command:

```text
schemas/model-plane-gateway-launch.schema.json
examples/model-plane/gateway-launch-profile.example.json
```

The profile maps to the gateway launch flags above:

| Profile key | Gateway flag |
| --- | --- |
| `gateway.state_dir` | `--state-dir` |
| `gateway.endpoint_id` | `--endpoint` |
| `gateway.host` | `--host` |
| `gateway.port` | `--port` |
| `gateway.checkpoint_mode` | `--checkpoint-mode` |
| `gateway.slot` | `--slot` |
| `gateway.default_prefill` | `--default-prefill` |
| `gateway.timeout_seconds` | `--timeout` |
| `gateway.max_bundle_bytes` | `--max-bundle-bytes` |
| `security.request_auth` | `--auth-token-file` or `--auth-token-env` |
| `security.bundle_signing` | `--signature-key-file`, `--signature-key-env`, `--signature-key-id`, and `--require-bundle-signature` |

The profile stores secret references only. It may say `source=file` and `ref=.capsule-gateway-token`; it must not store the token or signing key value.

Render the profile into gateway launch arguments:

```powershell
py -3 .\scripts\capsule_cli.py gateway command .\examples\model-plane\gateway-launch-profile.example.json --json
py -3 .\scripts\capsule_cli.py gateway check .\examples\model-plane\gateway-launch-profile.example.json --json
```

After launch, Model Plane should run the profile check. It reads `transport.status_url`, authenticates from `security.request_auth`, and requires the response's versioned `transport` object before enabling `.scap` upload/download controls.

For `gateway check`, relative file secret references are resolved from the profile directory.

## Endpoint Records

Endpoint settings are neither launch flags nor global config. They are durable endpoint records under:

```text
.capsules/endpoints/
```

They include:

- base URL
- endpoint type
- runtime build
- model reference
- model hash
- tokenizer hash
- context limit
- slot API fields

## Snapshot References

Hard local snapshot manifests use two different references:

| Field | Meaning |
| --- | --- |
| `storage.snapshot_ref` | Capsule-state-relative file path such as `threads/THREAD/snapshots/CAPSULE.bin`. |
| `storage.runtime_snapshot_ref` | Runtime-visible slot save/restore filename, which may be absolute or server-specific. |
| `storage.snapshot_digest` | Content digest metadata for verification and indexing. |

For v0, hard snapshot files are stored under the capsule state directory and referenced by store-relative path. They are not addressed by digest path yet. The digest is metadata, and missing snapshots still fall back to transcript replay.

## Help Surface

There is enough configuration surface for a real help view now:

- persistent storage config
- endpoint records
- gateway launch flags
- thread identity headers
- prefill selection
- gateway bundle transport
- bundle signature key handling
- gateway auth token handling
- Model Plane job packets
- storage stats, pinning, and GC

The most important rule for help text is to separate durable policy from launch-time wiring. Storage budget belongs in persistent config. Port, slot, endpoint, and checkpoint mode belong in launch profiles or command flags.
