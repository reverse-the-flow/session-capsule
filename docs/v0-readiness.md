# Standalone V0 Readiness

This document is the current readiness audit for the standalone Session Capsules v0 roadmap.

## Status

The standalone v0 roadmap is implementation-complete for the local harness and gateway scope described in `docs/roadmap.md`.

The repo has:

- project-local capsule state under `.capsules/`
- schema-backed thread ledgers, capsule manifests, endpoint records, config, Model Plane job packets, and gateway launch profiles
- CLI soft capsules, hard local `llama.cpp` slot save/restore, restore fallback, and shutdown checkpointing
- reusable prefill capsules
- `.scap` export/import with digest verification, redaction, signature support, and external age-compatible sealing
- gateway request-path integration for OpenAI-compatible clients
- gateway `.scap` upload/download, store-only upload, raw upload import, stored-bundle import, delete, CORS discovery, auth, and policy gates
- Model Plane job packets, gateway launch profile rendering/checking, required transport capability gating, and sealed-transfer metadata
- storage budget, pinning, stats, and garbage collection for hard snapshot cache artifacts
- state-reference and state-location policy
- identity discovery for generic clients, Open WebUI, opencode, Model Plane, and local UIs

## Verification

The readiness gate is:

```powershell
py -3 .\scripts\run_smoke_tests.py
```

That command covers:

- schema and example validation
- CLI help and discoverability
- fake `llama.cpp` endpoint doctor slot and runtime metadata probes
- hard checkpoint, resume, append-diff, shutdown, and failed-restore fallback
- prefill handling
- `.scap` export/import/verify/redaction/signing/sealing
- storage config, pinning, stats, and GC
- Model Plane job packets and gateway launch profiles
- gateway request-path behavior
- gateway bundle transport upload/download controls
- gateway auth, CORS, identity, endpoint readiness, and import policy discovery

## Deliberate Non-Goals

These are not missing standalone v0 work:

- hosted/provider-side sealed capsules
- user-carried runtime snapshots portable across model backends
- model weights inside capsule bundles
- passive watching of browser/app state
- replacing opencode generated provider configs before OpenCode exposes a provider-request/header hook or session-aware provider header template
- making Model Plane own model weights, live KV tensors, runtime slot layout, or the inference loop
- user-level/global state as the default storage location

## Readiness Criteria

Standalone v0 is ready when:

- the roadmap has no tracked open questions for standalone v0
- `py -3 .\scripts\run_smoke_tests.py` passes
- generated capsule state, bundles, snapshots, tokens, and run artifacts remain out of git
- the docs clearly describe what is implemented now versus what is future provider/runtime work

## Current Boundary

Session Capsules v0 is a local request-path harness and gateway for canonical thread state plus runtime-specific acceleration checkpoints.

It is not a portable model-weight format, a provider-side hidden-state API, a browser watcher, or a replacement for the inference server.
