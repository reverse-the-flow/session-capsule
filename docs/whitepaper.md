# Session Capsules

## Extending KV Reuse Across Agent Boundaries

Author draft: March 13, 2026

## Abstract

Modern LLM serving stacks optimize heavily within a single request, but many agent workflows are still inefficient across requests. Each tool call or loop iteration often resends a growing transcript, forcing the backend to rebuild internal state for context it already processed moments earlier. This creates structurally wasteful scaling for multi-step tasks.

This paper proposes Session Capsules: resumable inference artifacts that preserve prior model state across request boundaries. The immediate open-source implementation path is `llama.cpp` slot snapshotting. The broader architectural claim is that cross-request state resumption is a missing middle layer between stateless APIs and fully stateful provider-controlled sessions.

The main contribution is not a new transformer algorithm. It is an architecture for amortizing repeated prefill work in agentic loops, turning a replay-heavy workflow into one that pays mainly for new work. The hypothesis is that this can move repeated agent execution from roughly quadratic token replay toward linear incremental cost, while preserving user control and enabling future security envelopes such as signed or sealed capsules.

## 1. Problem

Agent systems often follow this pattern:

1. Build prompt from system instructions, history, tool traces, and task state.
2. Run inference.
3. Emit a tool call.
4. Receive tool result.
5. Rebuild the next prompt by replaying most or all prior history.
6. Repeat.

When history grows with each step, cumulative input replay grows roughly with the sum of all prior contexts. If context at step `k` is `n0 + gk`, then total replayed input across `S` steps is proportional to:

`sum(n0 + gk)` which is `O(S^2)`.

If the backend repeatedly performs long-context prefill on those prompts, repeated prefill work can scale even worse depending on the serving stack and sequence-length regime. Regardless of exact asymptotics, the architecture is wasteful because unchanged state is recomputed across request boundaries.

## 2. Claim

The optimization seam is the request boundary.

Serving systems already use caches, batching, and optimized attention kernels inside a request. Session Capsules extend that logic across requests by allowing previously computed state to be resumed rather than rebuilt.

The guiding principle is:

`prefill once, resume many`

Rather than treating every step as a fresh prompt, the system returns a resume artifact that represents the already-processed prefix. The next step sends that artifact plus only the new delta.

## 3. Definition

A Session Capsule is a resumable inference artifact that allows a serving system to continue generation from previously processed context with bounded recomputation.

A capsule may contain or reference:

- full KV state
- a compressed or quantized state snapshot
- a server-readable handle to ephemeral cached state
- a checkpoint plus a bounded hot cache

The definition is architectural, not format-specific. The ideal early prototype is full state save/restore because it is easiest to reason about and benchmark.

## 4. Reference Prototype

The most direct open implementation path is `llama.cpp` server slot persistence.

Conceptually:

1. Load model into `llama.cpp` server with slot support.
2. Prefill a conversation into slot `N`.
3. Save slot `N` to a snapshot file at a tool boundary.
4. Restore the snapshot later into a free slot.
5. Continue generation with new deltas and possibly different sampling settings.

Why `llama.cpp` is a good substrate:

- open source
- explicit slot abstraction
- snapshot save/restore support
- practical for local prototypes
- close enough to the real serving problem to produce meaningful measurements

## 5. System Model

### 5.1 World Structure

Relevant entities:

- model runtime
- tokenized prompt history
- internal inference state
- tool outputs
- agent controller
- user-carried or provider-issued resume artifact

Key constraint:

Internal state is runtime-specific. Portability across different model families or incompatible builds is not free.

### 5.2 Dynamics

Stateless loop:

- replay prompt
- rebuild state
- generate
- repeat

Capsule loop:

- restore state
- append delta
- generate
- emit updated capsule

The capsule path shifts cost from repeated transformer compute toward memory movement, serialization, and validation.

### 5.3 Attractors

Current serving attractor:

- stateless APIs
- easy horizontal scaling
- hidden provider-side state
- repeated replay overhead

Proposed attractor:

- resumable sessions
- bounded retention
- lower repeated prefill cost
- more explicit state lifecycle

## 6. Security And Trust Model

For local experiments, raw snapshots are sufficient.

For hosted inference, a credible sealed-capsule design likely needs:

- capsule version
- model/runtime fingerprint
- expiration timestamp
- size limits
- integrity signature
- optional encryption

The purpose is not only confidentiality. It is also preventing state tampering. If a user can arbitrarily edit low-level inference state, the system may become unsafe or inconsistent.

## 7. Portability Limits

Session Capsules are not automatically model-agnostic.

At minimum, compatibility usually depends on:

- model family
- tokenizer compatibility
- quantization/runtime build
- state layout expectations

A broader multi-model workflow is still possible, but not by assuming a single capsule works everywhere. A more realistic approach is overlapping checkpoints:

- keep the capsule specific to each runtime
- carry the canonical text diff across model switches
- rebuild only when necessary

This preserves the high-value property: state reuse where compatible, graceful fallback where not.

## 8. Compression Tiers

The capsule does not have to be one thing forever. A tiered design may be better:

- Gold capsule: full KV snapshot for exact resume
- Silver capsule: compressed checkpoint plus small hot cache
- Bronze capsule: canonical summarized state plus replay

This suggests a policy architecture rather than a single fixed artifact:

- use lighter checkpoints early when context is small
- switch to full exact capsules when replay waste dominates
- fall back to summary mode when compatibility breaks

## 9. What This Is Not

This is not a claim that KV state is always tiny.

In fact, full saved state can be much larger than raw text. That does not invalidate the idea. The point is to preserve expensive computation, not merely compress the transcript. The relevant tradeoff is:

- cost of transferring or loading saved state
versus
- cost of recomputing the state from text

For local inference or fast interconnects, resume can win early. For remote inference, the crossover point depends on context length, runtime speed, bandwidth, and compression quality.

## 10. Prototype Experiments

A useful first paper-ready benchmark suite would measure:

1. Replay vs restore latency at multiple context lengths
2. Slot save time and restore time
3. Snapshot size vs raw transcript size
4. Sensitivity to different quantizations and context sizes
5. Agent loop cumulative cost under replay vs capsule resume

Suggested scenarios:

- short loop: 2k initial context, 500-token growth
- medium loop: 8k initial context, 1k growth
- long loop: 32k initial context, 1k growth

Outputs worth publishing:

- latency table
- cumulative cost chart
- capsule size table
- failure cases and fallback behavior

## 11. Why This Is Worth Sharing

This idea is useful even if larger labs are already considering adjacent concepts. The value of a public prototype is not novelty theater. It is:

- a clear problem statement
- a runnable demonstration
- a vocabulary for discussing cross-request state reuse
- a baseline implementation others can critique or extend

The sentence worth proving is:

`Extend KV reuse across calls and collapse repeated agent replay into mainly incremental work.`

## 12. Near-Term Build Plan

1. Implement a local `llama.cpp` benchmark harness.
2. Collect measurements for save, restore, and replay.
3. Package the results into reproducible scripts and plots.
4. Add a sealed-capsule metadata envelope.
5. Publish a small technical write-up and repo link.

## Conclusion

Session Capsules frame a missing systems layer in LLM infrastructure. They do not replace model efficiency research, better kernels, or context compilers. They address a specific architectural waste pattern: repeated reconstruction of recently computed context across agent boundaries.

If the idea survives real measurements, metadata/versioning constraints, and restore overhead analysis, then it becomes more than a thought experiment. It becomes a practical serving pattern for agentic inference.

