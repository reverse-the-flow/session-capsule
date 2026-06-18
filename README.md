# Session Capsules

Session Capsules are a proposal for reducing agent inference waste by extending prompt/KV reuse across request boundaries.

The core claim is simple:

- Stateless agent loops repeatedly resend growing history.
- That creates roughly quadratic growth in billed input tokens over steps.
- It can create cubic-ish growth in repeated prefill work if long-context attention dominates.
- A resume artifact that restores prior model state can amortize that repeated work toward linear growth in the new work only.

This repository turns that idea into something shareable:

- a concise architecture write-up
- a white paper draft
- a small simulator for comparing replay vs capsule-style resumption
- a benchmark-first local MVP using `llama.cpp` slot save/restore

## Working Thesis

Current LLM APIs are mostly stateless at the request boundary. That is convenient for providers, but it is structurally wasteful for multi-step agent workloads. Session Capsules treat inference state as a resumable artifact.

At the implementation level, the most direct open-source substrate is `llama.cpp` server slot persistence:

- prefill a prompt once into a slot
- save the slot state at a tool or turn boundary
- restore that state later
- continue generation with new deltas and different sampling settings if needed

## Why This Matters

This idea is not mainly about saving bytes on the wire. It is about preserving expensive intermediate work.

- Replay path: resend text, rebuild state, repay prefill
- Capsule path: restore prior state, add only the delta, continue generation

That matters most for:

- long multi-step coding agents
- research loops with tool calls
- local inference on constrained hardware
- future hosted APIs that want lower-cost agent execution without permanent history retention

## Repository Layout

- [docs/whitepaper.md](/X:/Experiments/session-capsules/docs/whitepaper.md) - shareable paper draft
- [docs/help.md](/X:/Experiments/session-capsules/docs/help.md) - quick conceptual help and CLI help topic map
- [docs/protocol.md](/X:/Experiments/session-capsules/docs/protocol.md) - manifest, ledger, reload order, storage modes, and request-path integration model
- [docs/configuration.md](/X:/Experiments/session-capsules/docs/configuration.md) - persistent settings, launch flags, storage budget, pinning, and GC
- [docs/transport.md](/X:/Experiments/session-capsules/docs/transport.md) - gateway `.scap` upload/download API for Model Plane and local UI integration
- [docs/sealing.md](/X:/Experiments/session-capsules/docs/sealing.md) - sealed bundle transport, age-compatible backend, and key-reference policy
- [docs/integrations.md](/X:/Experiments/session-capsules/docs/integrations.md) - thin Open WebUI and opencode integration guidance for the local gateway
- [docs/opencode-native-hook.md](/X:/Experiments/session-capsules/docs/opencode-native-hook.md) - why generated opencode provider configs remain the supported path until a provider-request header hook exists
- [docs/model-plane.md](/X:/Experiments/session-capsules/docs/model-plane.md) - Model Plane boundary and job-packet contract
- [docs/verification.md](/X:/Experiments/session-capsules/docs/verification.md) - smoke-test command and verification boundary
- [docs/source-review.md](/X:/Experiments/session-capsules/docs/source-review.md) - distilled requirements from the original local Capsule Resume notes
- [docs/pwa-analysis.md](/X:/Experiments/session-capsules/docs/pwa-analysis.md) - structural analysis of the system and its leverage points
- [docs/benchmark-design.md](/X:/Experiments/session-capsules/docs/benchmark-design.md) - regime-safe benchmark plan for replay vs restore
- [docs/mvp-plan.md](/X:/Experiments/session-capsules/docs/mvp-plan.md) - smallest viable architecture, controller/runtime split, and ephemeral-runtime constraint
- [docs/roadmap.md](/X:/Experiments/session-capsules/docs/roadmap.md) - staged implementation path from CLI harness to local gateway and later integrations
- [docs/v0-readiness.md](/X:/Experiments/session-capsules/docs/v0-readiness.md) - standalone v0 readiness audit, verification gate, and deliberate non-goals
- [schemas/](/X:/Experiments/session-capsules/schemas) - draft JSON Schemas for capsule manifests, thread ledgers, and endpoint capabilities
- [examples/](/X:/Experiments/session-capsules/examples) - matching example ledger, manifest, endpoint capability, integration, Model Plane job, and gateway launch-profile documents
- [scripts/simulate_capsules.py](/X:/Experiments/session-capsules/scripts/simulate_capsules.py) - simple cost-growth simulator
- [scripts/llama_slot_workflow.ps1](/X:/Experiments/session-capsules/scripts/llama_slot_workflow.ps1) - prototype workflow for local `llama.cpp` slot save/restore
- [scripts/benchmark_llama_capsules.py](/X:/Experiments/session-capsules/scripts/benchmark_llama_capsules.py) - paired replay/save/restore benchmark harness that writes inspectable run folders
- [scripts/validate_schema_examples.py](/X:/Experiments/session-capsules/scripts/validate_schema_examples.py) - dependency-free sanity checks for the bundled schema examples
- [scripts/capsule_cli.py](/X:/Experiments/session-capsules/scripts/capsule_cli.py) - Stage 2 soft-capsule CLI for endpoint records, thread ledgers, transcripts, checkpoints, and inspection
- [scripts/capsule_gateway.py](/X:/Experiments/session-capsules/scripts/capsule_gateway.py) - local OpenAI-compatible gateway that manages thread restore, request deltas, and checkpointing in the request path
- [scripts/test_capsule_cli_fake_llamacpp.py](/X:/Experiments/session-capsules/scripts/test_capsule_cli_fake_llamacpp.py) - fake `llama.cpp` slot server smoke test for hard checkpoint, resume, shutdown job, and restore fallback commands
- [scripts/test_capsule_cli_export_import.py](/X:/Experiments/session-capsules/scripts/test_capsule_cli_export_import.py) - `.scap` bundle export/import smoke test
- [scripts/test_capsule_cli_model_plane_jobs.py](/X:/Experiments/session-capsules/scripts/test_capsule_cli_model_plane_jobs.py) - Model Plane job-packet smoke test for checkpoint, validate, export, shutdown planning, and gateway transport
- [scripts/test_capsule_gateway_fake_backend.py](/X:/Experiments/session-capsules/scripts/test_capsule_gateway_fake_backend.py) - fake backend smoke test for the local capsule gateway
- [data/scenarios/research_loop_small.json](/X:/Experiments/session-capsules/data/scenarios/research_loop_small.json) - example transcript-growth scenario for the benchmark harness

## Quick Start

Run the simulator:

```powershell
py -3 .\scripts\simulate_capsules.py --steps 30 --initial-context 2000 --growth-per-step 800 --output-tokens 300
```

Generate CSV output:

```powershell
py -3 .\scripts\simulate_capsules.py --csv .\data\scenario.csv
```

Run the local benchmark harness against a running `llama.cpp` server:

```powershell
py -3 .\scripts\benchmark_llama_capsules.py --scenario .\data\scenarios\research_loop_small.json --base-url http://localhost:8080
```

Treat the runtime as ephemeral: keep the `data/runs/...` outputs and discard the sandboxed test container or instance after the series finishes.

If your build does not support prompt-only evaluation with `max_tokens=0`, rerun with `--max-tokens 1` and treat the result as a noisier pilot rather than the clean headline benchmark.

Review the paper:

- [docs/whitepaper.md](/X:/Experiments/session-capsules/docs/whitepaper.md)
- [docs/protocol.md](/X:/Experiments/session-capsules/docs/protocol.md)
- [docs/benchmark-design.md](/X:/Experiments/session-capsules/docs/benchmark-design.md)
- [docs/mvp-plan.md](/X:/Experiments/session-capsules/docs/mvp-plan.md)
- [docs/roadmap.md](/X:/Experiments/session-capsules/docs/roadmap.md)

Validate the schema examples:

```powershell
py -3 .\scripts\validate_schema_examples.py
```

Show conceptual CLI help:

```powershell
py -3 .\scripts\capsule_cli.py help
py -3 .\scripts\capsule_cli.py help --topics
py -3 .\scripts\capsule_cli.py help storage
```

Create persistent capsule config:

```powershell
py -3 .\scripts\capsule_cli.py config init
py -3 .\scripts\capsule_cli.py state info
py -3 .\scripts\capsule_cli.py config set storage.max_bytes 50GB
py -3 .\scripts\capsule_cli.py config set storage.min_free_bytes 20GB
```

Run the soft-capsule CLI lifecycle without a loaded model:

```powershell
py -3 .\scripts\capsule_cli.py endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080
py -3 .\scripts\capsule_cli.py thread start --endpoint local-llamacpp --name research-loop-small
py -3 .\scripts\capsule_cli.py thread append --thread research-loop-small --role user --content "Initial benchmark request."
py -3 .\scripts\capsule_cli.py checkpoint --thread research-loop-small --soft
py -3 .\scripts\capsule_cli.py inspect --thread research-loop-small
```

Run the local hard-capsule lifecycle against a `llama.cpp` endpoint with slot save/restore:

```powershell
py -3 .\scripts\capsule_cli.py endpoint doctor local-llamacpp --strict
py -3 .\scripts\capsule_cli.py endpoint matrix --json
py -3 .\scripts\capsule_cli.py thread append --thread research-loop-small --role user --content "Runtime-visible prompt delta."
py -3 .\scripts\capsule_cli.py checkpoint --thread research-loop-small --hard --slot 0
py -3 .\scripts\capsule_cli.py thread append --thread research-loop-small --role tool --content "Tool output after checkpoint."
py -3 .\scripts\capsule_cli.py resume --thread research-loop-small --slot 1 --append-diff
py -3 .\scripts\capsule_cli.py shutdown --thread research-loop-small --slot 1 --force
```

`endpoint doctor` records `/slots` probe evidence in the endpoint record: response shape, sample keys, candidate slot identity fields, configured chat slot field, and visible `n_ctx` / `is_processing` fields. It also performs a non-fatal runtime metadata probe, defaulting to `/props`, to capture live build/model/context fields when the endpoint exposes them. `endpoint matrix --json` aggregates those endpoint records into a compatibility report that Model Plane or a launcher can read before enabling hard capsule controls.

If the model server runs in Docker, pass `--runtime-filename` when saving hard checkpoints so the filename is visible from inside the container's `--slot-save-path` mount.

If hard restore fails, `resume --append-diff` marks that capsule `restore_failed`, replays the canonical transcript into the slot with `cache_prompt=false`, and saves a replacement hard checkpoint. Without `--append-diff`, it reports the replay fallback plan without mutating the runtime slot.

Create and use a reusable prefill capsule:

```powershell
py -3 .\scripts\capsule_cli.py prefill create --endpoint local-llamacpp --name user_default --input .\user_prefill.md --soft
py -3 .\scripts\capsule_cli.py prefill diff --name user_default --input .\user_prefill.md
py -3 .\scripts\capsule_cli.py thread start --endpoint local-llamacpp --prefill user_default --name research-with-prefill
```

For a hard local prefill, use `--hard --slot N` after confirming the endpoint with `endpoint doctor`.

Export and import a thread bundle:

```powershell
py -3 .\scripts\capsule_cli.py export --thread research-loop-small --out .\research-loop-small.scap --dry-run
py -3 .\scripts\capsule_cli.py export --thread research-loop-small --out .\research-loop-small.scap
py -3 .\scripts\capsule_cli.py verify .\research-loop-small.scap
py -3 .\scripts\capsule_cli.py inspect --bundle .\research-loop-small.scap
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop-small.scap --preset metadata-only
py -3 .\scripts\capsule_cli.py import .\research-loop-small.scap
py -3 .\scripts\capsule_cli.py import .\research-loop-small.scap --thread-id research-loop-copy
```

By default, `.scap` export is ledger-only: it includes endpoint metadata, thread ledger, transcript, capsule manifests, prefill sources, and per-entry file digests, but omits hard snapshot blobs. Add `--include-snapshots` only when intentionally moving same-runtime local snapshot files. Add `--redact-transcript` to create a metadata-only bundle that omits transcript and prefill source text; this is useful for inspection, but it is not encryption.

`inspect --bundle` reports the bundle's share/import posture: plaintext transcript or prefill source content, snapshot inclusion, redaction, signing, encryption status, and whether trusted transport is required.

`bundle-policy` turns that posture into an exit-code gate for scripts and launchers. `--preset metadata-only` rejects transcript/prefill plaintext and snapshots. `--preset signed-metadata-only` also requires a signature. `--preset sealed` requires an encrypted envelope.

Import warns when the bundle endpoint id already exists locally with different runtime, model, tokenizer, context, slot, or URL metadata.

Optional HMAC signing uses an explicit key source and does not store secrets in `.capsules`:

```powershell
py -3 .\scripts\capsule_cli.py export --thread research-loop-small --out .\research-loop-small.scap --signature-key-file .\capsule-signing.key --signature-key-id local
py -3 .\scripts\capsule_cli.py verify .\research-loop-small.scap --signature-key-file .\capsule-signing.key --require-signature
```

Sealed envelopes delegate encryption to an external age-compatible command. Public recipient files may be referenced from project launch policy; private identity files should stay outside `.capsules`.

```powershell
py -3 .\scripts\capsule_cli.py seal .\research-loop-small.scap --out .\research-loop-small.sealed.scap --age-recipient-file .\.capsules\security\recipients\local.agepub
py -3 .\scripts\capsule_cli.py bundle-policy .\research-loop-small.sealed.scap --preset sealed
py -3 .\scripts\capsule_cli.py unseal .\research-loop-small.sealed.scap --out .\research-loop-small.unsealed.scap --age-identity C:\Users\you\.config\age\keys.txt
```

The gateway can apply the same policy to upload/download transport:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --signature-key-file .\capsule-signing.key --signature-key-id local --require-bundle-signature
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --bundle-policy-preset metadata-only
```

Gateway import policy is server-side. It rejects raw uploads and stored-bundle imports before extraction when the bundle does not satisfy the selected policy.

If the gateway is bound beyond local-only use, require a request token:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --auth-token-file .\capsule-gateway-token
```

If browser-hosted Model Plane controls call upload/download directly, allow that exact UI origin:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --cors-allow-origin http://127.0.0.1:3000
```

Inspect and clean local hard capsule storage:

```powershell
py -3 .\scripts\capsule_cli.py stats
py -3 .\scripts\capsule_cli.py pin --thread research-loop-small
py -3 .\scripts\capsule_cli.py gc --dry-run
py -3 .\scripts\capsule_cli.py gc --apply
```

Run the fake `llama.cpp` hard-path smoke test:

```powershell
py -3 .\scripts\test_capsule_cli_fake_llamacpp.py
py -3 .\scripts\test_capsule_cli_export_import.py
py -3 .\scripts\test_capsule_cli_storage_gc.py
py -3 .\scripts\test_capsule_cli_model_plane_jobs.py
py -3 .\scripts\test_capsule_gateway_fake_backend.py
```

Or run all dependency-free checks:

```powershell
py -3 .\scripts\run_smoke_tests.py
```

Run the local OpenAI-compatible capsule gateway:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode soft
```

For a local `llama.cpp` endpoint with slot save/restore, use hard mode:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode hard --slot 0
```

Point an OpenAI-compatible client at `http://127.0.0.1:8765/v1`. The v0 gateway is non-streaming, so clients must send `stream=false`. Optional request headers are:

- `X-Capsule-Thread`: stable app or workspace thread id
- `X-Capsule-Workspace`: workspace/project id for ledger metadata
- `X-Capsule-Prefill`: named prefill capsule to attach when a thread is first created

The status endpoint advertises the full identity contract, including native Open WebUI and opencode headers:

```text
GET /api/capsules/status -> identity
```

The gateway also exposes local `.scap` bundle transport:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles
POST   /api/capsules/bundles
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
DELETE /api/capsules/bundles/{bundle_id}
```

Model Plane should read `/api/capsules/status` first and use the response's `transport` object to discover endpoint paths, upload size, content type, auth policy, signing policy, CORS policy, and enabled bundle capabilities. Launch profiles can list `transport.required_capabilities`; `gateway check` verifies each required capability before Model Plane enables profile-dependent upload/download controls.

Launch profiles can also include `security.bundle_sealing` with a public `age_recipient_file`. `gateway command --json` returns a `bundle_sealing.seal_command_template` for sealed user-carried transfers; private age identity files stay outside the profile and outside `.capsules`.

Bundles are stored under `.capsules/bundles/`. Export defaults to ledger-only; hard snapshots require `include_snapshots=true`.
Store-only upload places a verified `.scap` in the bundle store without creating thread state. Imports can target a new local thread id with `thread_id` in JSON control calls or `X-Capsule-Import-Thread` on raw `.scap` upload-imports.

Open WebUI and opencode setup examples live in:

- [examples/integrations/open-webui.env.example](/X:/Experiments/session-capsules/examples/integrations/open-webui.env.example)
- [examples/integrations/opencode.capsule-provider.jsonc](/X:/Experiments/session-capsules/examples/integrations/opencode.capsule-provider.jsonc)
- [examples/integrations/start-opencode-capsule.ps1](/X:/Experiments/session-capsules/examples/integrations/start-opencode-capsule.ps1)

Generate an opencode provider config with concrete capsule identity headers:

```powershell
py -3 .\scripts\capsule_cli.py integration opencode-config --workspace . --session default --prefill user_default --out .\.capsules\integrations\opencode.generated.json
```

Run a Model Plane job packet through the standalone harness:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\checkpoint-thread.example.json --dry-run
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\shutdown-thread.example.json --dry-run
```

Render a Model Plane gateway launch profile:

```powershell
py -3 .\scripts\capsule_cli.py gateway command .\examples\model-plane\gateway-launch-profile.example.json --json
py -3 .\scripts\capsule_cli.py gateway check .\examples\model-plane\gateway-launch-profile.example.json --json
```

`gateway check --json` verifies the running transport contract and reports `endpoint_verified`. Hard checkpoint profiles require the gateway status to advertise a ready endpoint compatibility record from a successful slot probe.

Gateway upload/download can also be driven by Model Plane job packets:

```powershell
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-export-bundle.example.json --dry-run
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\export-thread.example.json --signature-key-file .\capsule-signing.key --signature-key-id local
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-store-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py --state-dir .\.capsules job run .\examples\model-plane\gateway-download-bundle.example.json --gateway-auth-token-file .\capsule-gateway-token
```

Or call the running gateway directly from the standalone CLI:

```powershell
py -3 .\scripts\capsule_cli.py gateway status --url http://127.0.0.1:8765 --auth-token-file .\capsule-gateway-token --json
py -3 .\scripts\capsule_cli.py gateway export --url http://127.0.0.1:8765 --thread research-loop --bundle-id research-loop --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway download --url http://127.0.0.1:8765 --bundle-id research-loop --out .\research-loop.scap --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway store --url http://127.0.0.1:8765 --bundle .\research-loop.scap --bundle-id stored-research-loop --auth-token-file .\capsule-gateway-token
py -3 .\scripts\capsule_cli.py gateway upload --url http://127.0.0.1:8765 --bundle .\research-loop.scap --bundle-id uploaded-research-loop --thread-id research-loop-copy --auth-token-file .\capsule-gateway-token
```

Benchmark runs are written to `data/runs/<timestamp>-<label>/` with:

- `manifest.json` for provenance and configuration
- `events.jsonl` for step-by-step raw measurements
- `summary.json` for aggregate comparison
- `capsules/` for saved slot snapshots

## Prototype Scope

This repo does not claim production-ready hosted portability yet. The first useful prototype is narrower:

1. Single model
2. Single runtime per test series
3. Sandboxed local or ephemeral test container execution
4. `llama.cpp` slot snapshots as the persistence spine
5. Fallback to normal replay when metadata mismatches

## Near-Term Roadmap

The canonical staged implementation plan lives in [docs/roadmap.md](/X:/Experiments/session-capsules/docs/roadmap.md).

The short version:

1. Make the repo uploadable without private context.
2. Define capsule manifest, thread ledger, and endpoint capability schemas.
3. Build a CLI soft-capsule ledger.
4. Add local hard capsules through `llama.cpp` slot save/restore.
5. Add user prefill capsules and `.scap` export/import.
6. Add a local OpenAI-compatible capsule gateway.
7. Add gateway upload/download transport for `.scap` bundles.
8. Add native app integrations and Model Plane coordination only after the standalone primitive works.
