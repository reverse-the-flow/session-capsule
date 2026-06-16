# Benchmark Design

## Goal

Measure replay versus capsule resume only while the runtime stays in one stable memory regime.

The benchmark must not mix:

- fully-in-VRAM execution
- partial spill into system RAM
- different batch or slot occupancy modes
- different quantizations or runtime builds

If the run changes regime mid-benchmark, discard it.

## Core Principle

We are not benchmarking "best possible wall clock under anything that happens."
We are benchmarking a controlled question:

`Given a fixed runtime regime, is restoring prior state cheaper than replaying the same context?`

That means every compared run must hold constant:

- model file
- quantization
- `llama.cpp` build
- context size limit
- slot count
- GPU offload settings
- parallelism settings
- prompt shape
- output length
- machine background load

## Regimes

Define regimes explicitly before collecting data.

### Regime A: Fully In VRAM

- model weights fit in VRAM
- active KV for the tested context fits in VRAM
- no observable fallback to system RAM paging
- GPU memory usage stays below a safety ceiling for the full run

This is the cleanest first benchmark and should be the default publication target.

### Regime B: Stable Mixed VRAM Plus RAM

- weights and/or KV partly live outside VRAM
- the runtime remains consistently in this same mixed mode for all compared runs
- no additional spill event occurs mid-run

This regime is valid only if it is intentionally selected from the start and remains stable. It should not be mixed with Regime A results.

### Invalid Regime Change

Any of the following invalidates the run:

- VRAM usage crosses a spill threshold during the run
- tokens per second collapses because the runtime moved from VRAM-resident KV to host-backed KV
- slot occupancy changes the memory mode relative to the paired comparison run
- another process changes available VRAM enough to alter placement

## Safety Rule

Never benchmark near the cliff.

Use a safety margin so the tested configuration stays comfortably inside the intended regime. A good practical rule is:

- choose a context length that leaves at least 10 to 20 percent VRAM headroom in Regime A
- if you want to study the cliff, do that as a separate experiment, not inside the main benchmark

## Variables To Hold Fixed

For each benchmark series, record and freeze:

- GPU model
- VRAM capacity
- CPU model
- system RAM
- OS
- `llama.cpp` commit or build identifier
- model name and exact file hash
- quantization
- `-c` context limit
- `-ngl` GPU offload setting
- `-np` slot count
- flash attention and other runtime flags
- prompt template
- target generation length
- temperature and seed

## Benchmark Questions

The first benchmark set should answer four narrow questions.

1. How long does replay take for a fixed prefix length?
2. How long does restore take for the equivalent saved slot state?
3. How large is the saved capsule file at that prefix length?
4. Across a multi-step loop, how much cumulative time is avoided by restore versus replay?

## Measurement Units

Collect at least these metrics:

- prompt tokens
- generated tokens
- replay wall-clock milliseconds
- restore wall-clock milliseconds
- save wall-clock milliseconds
- time to first token after replay
- time to first token after restore
- capsule file bytes
- GPU memory used before run
- GPU memory peak during run
- CPU RAM used before run
- CPU RAM peak during run

If available from the runtime logs, also capture:

- prompt throughput tokens per second
- decode throughput tokens per second
- tokens cached
- slot metadata

## Recommended Experimental Shape

### Phase 1: Single Prefix Length Sanity Check

Pick one prefix size that is clearly safe in VRAM.

For example:

- 8k prefix
- fixed output length such as 128 tokens
- 20 replay trials
- 20 restore trials

This confirms the harness works and the variance is acceptable.

### Phase 2: Context Sweep Within One Regime

Run a sweep like:

- 2k
- 4k
- 8k
- 12k
- 16k

Only keep values that stay in the same regime for the full sweep. If 16k spills but 12k does not, then the clean Regime A sweep stops at 12k.

### Phase 3: Agent Loop Benchmark

Use a scripted loop with fixed delta growth per step.

Example:

- initial prefix: 4k
- per-step added tool/result delta: 600 tokens
- steps: 12
- compare:
  - replay full transcript each step
  - save and restore slot state each step

Measure cumulative wall time and cumulative prompt-processing time.

## Instrumentation

At minimum, record memory state before and after each trial.

Useful signals:

- `nvidia-smi` sampled once per second during the benchmark
- server logs for prompt timing, cache events, and slot activity
- process-level RAM usage for the server process
- capsule file size on disk immediately after save

Save raw logs beside the summarized CSV so rejected runs can be audited later.

## Pairing Rules

Each replay run and restore run must be paired.

That means:

- same prompt text
- same output limit
- same slot count
- same server state except for the tested action
- same model warmup condition

Do not compare a cold first run against a warm restore and call it a fair result.

Recommended pattern:

1. warm server
2. run a non-measured priming request
3. measure replay trial
4. reset to neutral state
5. measure restore trial
6. alternate order across repetitions to reduce order bias

## Warmup And Order Control

Use warmup before any recorded trials.

Then either:

- alternate replay and restore order each repetition, or
- randomize order using a pregenerated schedule

This helps avoid systematic bias from thermal state, filesystem cache, or runtime warmup.

## Spill Detection And Rejection Policy

A run is rejected if any of these happen:

- GPU memory peak exceeds the predefined safe ceiling
- host RAM jumps in a way that indicates new spill behavior relative to the baseline
- throughput drops beyond a predefined regime-change threshold, for example more than 25 percent from the stable median for that series
- the server logs indicate context truncation, slot eviction, or memory fallback

Keep rejected runs in a log, but exclude them from headline charts.

## Reporting Layout

The simplest convincing report is:

1. Environment table
2. Regime definition table
3. Context sweep table
4. Agent loop cumulative chart
5. Capsule file size table
6. Rejected-runs appendix

## Headline Benchmark To Publish First

If you only publish one result first, publish this:

- one model
- one quantization
- one GPU
- one clearly in-VRAM regime
- one 10 to 15 step agent loop
- cumulative replay time versus cumulative restore time
- capsule size per step

That result is easy to explain and hard to dismiss.

## Suggested First Thresholds

Use these as starting defaults, then tighten them after a pilot run:

- VRAM safety ceiling: 85 percent of physical VRAM
- run rejection on greater than 25 percent throughput collapse relative to series median
- minimum repetitions per point: 10
- separate plots for each regime

## Practical Conclusion

You are exactly right that spill would contaminate the benchmark.

So the benchmark should not ask "what happens eventually if context keeps growing forever?"
It should ask:

- within fully-in-VRAM operation, what is the restore advantage?
- within intentionally mixed memory operation, what is the restore advantage?
- where is the crossover boundary between those regimes?

Those are three separate experiments, not one.
