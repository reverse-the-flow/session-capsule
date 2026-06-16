# PWA Analysis

## Target System

Session Capsules for cross-request LLM state reuse in multi-step agent workflows.

## World Structure

- Entities: model runtime, slot state, prompt transcript, tool outputs, agent controller, capsule artifact, validation metadata
- Constraints: runtime-specific state layouts, context window limits, bandwidth limits, version mismatch risk, security boundaries
- State variables: current context length, number of loop steps, saved state size, restore latency, replay latency, compatibility status

## Dynamics

- Stateless regime repeatedly rebuilds prompt-derived state from text.
- Capsule regime restores prior state and appends only the new delta.
- Longer loops amplify replay waste.
- Faster local storage or interconnects improve capsule attractiveness.
- Compression reduces transfer cost but can increase restore complexity or reduce fidelity.

## Attractors And Regimes

- Current regime: stateless APIs with repeated replay, simple infra, hidden provider-side caching
- Proposed regime: explicit resumable state with bounded retention and graceful fallback
- Dominant attractor today: operational simplicity beats state portability
- Potential regime shift: agent workloads become large enough that cross-request replay waste becomes intolerable

## Measure

- Weighted possibilities:
  - local single-model prototype is highly reachable
  - hosted sealed capsules are plausible but policy-heavy
  - model-agnostic full-state portability is low-probability near term
- Assumptions:
  - multi-step agents remain a growing workload class
  - prefill waste is material enough to justify stateful infrastructure
  - restore overhead is lower than repeated replay beyond a context crossover

## Information

- Observables: slot save latency, restore latency, context length, transcript size, snapshot size, cumulative step cost
- Hidden variables: exact runtime internals, real provider cost curves, optimal checkpoint tier thresholds
- Uncertainties:
  - how portable state can be across builds
  - how much compression preserves useful behavior
  - where the replay-vs-resume crossover happens on different hardware

## Agency

- Agents:
  - runtime maintainers
  - local power users
  - hosted inference providers
  - agent framework authors
- Actions:
  - add snapshot save/restore APIs
  - seal and sign capsules
  - build hybrid checkpoint policies
  - benchmark replay against resumption
- Control points:
  - request boundary
  - tool-call boundary
  - session scheduling
  - metadata validation

## Selection

- Selection pressures:
  - lower serving cost
  - lower latency
  - easier agent scaling
  - privacy-preserving bounded retention
- Retention mechanisms:
  - open-source prototypes
  - reproducible benchmarks
  - integration into agent runtimes

## Narrative And Interpretation

- Dominant frame now: prompt replay is just how chat APIs work
- Alternative frame: stateless replay is a temporary systems convenience, not an optimal architecture
- Surprise point: saved state may be larger than text yet still be economically superior
- Meaning conflict: portability and user ownership pull one way, runtime-specific state fidelity pulls the other

## Leverage Points

- Prove the crossover with real measurements.
- Start with `llama.cpp` slots because the implementation seam already exists.
- Treat capsules as a tiered policy, not a single artifact type.
- Build graceful fallback as a first-class feature, not an afterthought.

## Failure Modes

- Restore overhead is not low enough to beat replay in realistic remote scenarios.
- Snapshot compatibility breaks too often to be practical.
- Security envelope complexity overwhelms the cost benefit.
- The prototype proves only local single-user value and does not generalize cleanly.

