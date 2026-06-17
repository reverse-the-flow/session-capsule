# Help

## Mental Model

Session Capsules separate canonical conversation state from runtime acceleration state.

```text
endpoint   = where the model server lives
thread     = canonical transcript plus capsule ledger
capsule    = checkpoint manifest, optionally with a hard snapshot
prefill    = reusable root context
gateway    = local OpenAI-compatible request-path layer
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
- `storage`
- `bundles`
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

These are good Model Plane launch-profile fields.

## Storage Safety

GC deletes only eligible hard snapshot blobs.

It does not delete:

- transcripts
- thread ledgers
- manifests
- soft checkpoints
- pinned thread capsules

If GC deletes a hard snapshot blob, the ledger link is marked `missing` and transcript replay remains the fallback.

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

## Model Plane

Model Plane should supervise the gateway, not absorb it.

Model Plane owns launch profiles, lifecycle, health checks, and job routing. The gateway owns the request path and capsule restore/checkpoint behavior. The model runtime owns model weights, live KV cache, slots, and generation.
