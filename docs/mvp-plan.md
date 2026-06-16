# Session Capsules MVP Plan

## Problem Framing

The repository already establishes the core claim: repeated transcript replay wastes inference work in multi-step agent loops. The smallest practical MVP is not a generalized capsule platform. It is a reproducible local benchmark that shows whether `llama.cpp` slot save/restore reduces repeated prompt work under one stable runtime regime, inside an ephemeral sandboxed test runtime, while persisting only the benchmark outputs.

## Smallest Viable Architecture

### Workflow

1. Start a sandboxed ephemeral test container or instance for the benchmark run.
2. Load a fixed benchmark scenario from a JSON file.
3. Warm the server once outside the measured loop.
4. Prefill and save a seed slot for the shared prompt prefix.
5. For each benchmark step:
   - run a replay request against the full accumulated transcript
   - restore the prior slot snapshot
   - run the same accumulated transcript against the restored slot with cache reuse enabled
   - save the updated slot snapshot for the next step
6. Destroy the ephemeral runtime after the run finishes.
7. Keep only the run folder with:
   - `manifest.json`
   - `events.jsonl`
   - `summary.json`
   - saved capsule files

### Why This Is The Right MVP

- It is testable on one local `llama.cpp` server or one ephemeral test container.
- It keeps controller logic outside the runtime.
- It preserves provenance and handoff continuity in files.
- It saves results, not the container or runtime instance.
- It does not assume cross-model portability, semantic routing, or a sealed production capsule format yet.

## Controller Versus Runtime

### Controller / Orchestrator Code

- launch or target the sandboxed ephemeral runtime
- load scenario files and benchmark settings
- build the per-step transcript sequence
- enforce replay versus restore trial structure
- record provenance, ambiguity notes, timings, and artifacts
- reject or flag runs when assumptions do not hold
- summarize outcomes for later model or human review

### Runtime / Model Code

- tokenize and evaluate prompts
- own slot residency and slot snapshot format
- execute save and restore operations
- report timings and usage data
- expose an interface that can live inside an ephemeral sandboxed instance
- terminate cleanly without being treated as durable state

### Boundary Rule

The controller decides *what to measure*, *what to persist*, and *when to destroy the runtime*. The runtime decides *how state is represented and resumed* while the instance is alive. The MVP should not blur those responsibilities.

## Evaluation Plan

Use one stable memory regime per series and compare:

1. replay request wall time for each accumulated transcript
2. restore wall time for the paired capsule step
3. save wall time after the paired capsule step
4. total capsule-path wall time: restore plus completion plus save
5. prompt-processing timings and prompt-token counts when the server exposes them
6. whether the run folder is sufficient to analyze the result after the ephemeral runtime is gone

Success criteria for the first MVP:

- the harness produces a complete run folder without manual bookkeeping
- replay and capsule paths are paired on the same scenario and prompt history
- the summary clearly separates replay cost from restore and save overhead
- the runtime can be discarded without losing benchmark evidence
- another model can inspect the run folder and continue analysis without hidden context

## Top 3 Risks Or Blockers

1. Prompt-only evaluation may not behave consistently across `llama.cpp` builds.
   The harness assumes `max_tokens=0` or equivalent prompt-only execution is valid for the cleanest first benchmark.
2. Replay measurements may be contaminated if the runtime reuses cache unexpectedly.
   The harness exposes replay reset controls, but the exact behavior still depends on the server build.
3. Ephemeral runtime setup may be heavier than the benchmark itself on some machines.
   The runtime boundary should be isolated, but container spin-up cost should not be mixed into the headline replay-versus-restore measurements.

## Recommended Next Artifact

Create a task-scoped runtime profile for an ephemeral benchmark instance, for example a `Dockerfile`, `compose.yaml`, or `POLICY.yaml` that mounts only the run-output directory as durable storage and destroys the container after each series. That keeps the next layer aligned with your sandbox-first requirement before expanding reporting.

## Provenance And Assumptions

- Source paths:
  - `README.md`
  - `docs/whitepaper.md`
  - `docs/benchmark-design.md`
  - `scripts/llama_slot_workflow.ps1`
- Transformation applied:
  - turned the concept repo into a benchmark-first MVP plan with explicit controller/runtime boundaries and an ephemeral-runtime persistence rule
- Why this change was made:
  - the repo had theory and a workflow sketch, but no inspectable local harness for empirical replay versus restore runs or a durable-results-only rule
- Ambiguity notes:
  - the live `llama.cpp` request field for slot selection varies by build, so the harness keeps the slot field configurable
  - the clean first benchmark assumes prompt-only evaluation is available
  - the exact container or sandbox mechanism is still open and should stay separate from the measurement logic
