# Model Plane Coordination

## Boundary

Model Plane should coordinate capsule-capable work. It should not become the inference backend.

Model Plane can own:

- endpoint registry
- endpoint capability cache
- thread and capsule registry
- job packets
- routing and fallback policy

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

## Fallback

Every job must be safe to degrade:

- `resume_thread` falls back only through the existing CLI restore/replay behavior.
- `checkpoint_thread` can use `mode=soft` when no runtime slot is available.
- `export_thread` omits hard snapshots unless explicitly requested.
- `validate_capsule` can report a missing local snapshot without invalidating the canonical transcript.

The transcript remains the source of truth. Capsules remain acceleration artifacts.
