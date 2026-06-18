# Source Review

## Reviewed Local Seed Notes

The original local `Capsule Resume` notes were reviewed as seed material. They are not copied into this repo because they are raw chat exports and are not suitable as public project source files.

Durable requirements extracted from those notes:

- The first hard capsule substrate should be `llama.cpp` slot save/restore.
- Capsules should not contain model weights.
- Runtime snapshots are compatibility-bound to model, tokenizer, runtime build, context limit, and slot format.
- Generation settings such as temperature, seed, top-p, and token budget are not part of the saved prefix state and can vary after restore.
- Full snapshots plus transcript diffs are the right initial implementation. Binary KV diffs are a later optimization.
- User/project prefill capsules are useful root states for repeated context.
- Bundle integrity, signatures, encryption, and user-carried sealed blobs are important capabilities, but encryption/sealing should not block the local MVP.
- Passive watching is not enough for reliable resume. The capsule layer belongs in the request path.

## Current Repository Coverage

- `scripts/capsule_cli.py` implements local endpoint records, thread ledgers, transcript append, soft checkpoints, hard slot checkpoints, resume, shutdown, prefill capsules, `.scap` export/import/verify, and Model Plane job packets.
- `scripts/capsule_gateway.py` implements the local OpenAI-compatible request-path gateway.
- `docs/protocol.md` records manifest, ledger, reload order, storage modes, gateway behavior, and Model Plane job packets.
- `docs/integrations.md` keeps Open WebUI and opencode integration thin and client-facing.
- `docs/model-plane.md` keeps Model Plane coordination separate from runtime slots and model state.
