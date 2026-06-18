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

## Secret Inputs

Signature keys are secret inputs, not persistent settings:

| Setting | Example | Why |
| --- | --- | --- |
| `--signature-key-file` | `.capsule-signing.key` | Reads a local signing key for export/import/verify without storing it in `.capsules`. |
| `--signature-key-env` | `CAPSULE_SIGNING_KEY` | Reads a signing key from the process environment. |
| `--signature-key-id` | `local` | Non-secret label written into signed bundle metadata. |

For the gateway, these are launch-profile values. Do not put signing keys in `settings.json`, endpoint records, or Model Plane job packets.

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

## Help Surface

There is enough configuration surface for a real help view now:

- persistent storage config
- endpoint records
- gateway launch flags
- thread identity headers
- prefill selection
- gateway bundle transport
- bundle signature key handling
- Model Plane job packets
- storage stats, pinning, and GC

The most important rule for help text is to separate durable policy from launch-time wiring. Storage budget belongs in persistent config. Port, slot, endpoint, and checkpoint mode belong in launch profiles or command flags.
