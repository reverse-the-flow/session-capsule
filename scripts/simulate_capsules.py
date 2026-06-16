#!/usr/bin/env python3
"""Compare cumulative replay cost against capsule-style resumption."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StepMetrics:
    step: int
    context_tokens: int
    replay_input_tokens: int
    replay_prefill_units: int
    capsule_delta_tokens: int
    capsule_prefill_units: int


def build_metrics(
    steps: int,
    initial_context: int,
    growth_per_step: int,
    output_tokens: int,
    restore_overhead_tokens: int,
) -> list[StepMetrics]:
    metrics: list[StepMetrics] = []
    for step in range(1, steps + 1):
        context_tokens = initial_context + (step - 1) * growth_per_step
        replay_input_tokens = context_tokens
        replay_prefill_units = context_tokens * context_tokens

        # Capsule mode pays only for the new delta plus a fixed restore overhead proxy.
        capsule_delta_tokens = growth_per_step + output_tokens + restore_overhead_tokens
        if step == 1:
            capsule_delta_tokens = context_tokens + output_tokens
        capsule_prefill_units = capsule_delta_tokens * capsule_delta_tokens

        metrics.append(
            StepMetrics(
                step=step,
                context_tokens=context_tokens,
                replay_input_tokens=replay_input_tokens,
                replay_prefill_units=replay_prefill_units,
                capsule_delta_tokens=capsule_delta_tokens,
                capsule_prefill_units=capsule_prefill_units,
            )
        )
    return metrics


def write_csv(path: Path, metrics: list[StepMetrics]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "step",
                "context_tokens",
                "replay_input_tokens",
                "replay_prefill_units",
                "capsule_delta_tokens",
                "capsule_prefill_units",
            ],
        )
        writer.writeheader()
        for row in metrics:
            writer.writerow(row.__dict__)


def format_int(value: int) -> str:
    return f"{value:,}"


def print_summary(metrics: list[StepMetrics], input_price_per_m: float) -> None:
    total_replay_tokens = sum(item.replay_input_tokens for item in metrics)
    total_replay_prefill = sum(item.replay_prefill_units for item in metrics)
    total_capsule_tokens = sum(item.capsule_delta_tokens for item in metrics)
    total_capsule_prefill = sum(item.capsule_prefill_units for item in metrics)

    replay_cost = total_replay_tokens / 1_000_000 * input_price_per_m
    capsule_cost = total_capsule_tokens / 1_000_000 * input_price_per_m

    token_reduction = 0.0
    prefill_reduction = 0.0
    if total_replay_tokens:
        token_reduction = 1 - (total_capsule_tokens / total_replay_tokens)
    if total_replay_prefill:
        prefill_reduction = 1 - (total_capsule_prefill / total_replay_prefill)

    print("Scenario")
    print(f"  steps: {metrics[-1].step}")
    print(f"  final context: {format_int(metrics[-1].context_tokens)} tokens")
    print()
    print("Replay")
    print(f"  cumulative input tokens: {format_int(total_replay_tokens)}")
    print(f"  synthetic prefill units: {format_int(total_replay_prefill)}")
    print(f"  estimated input cost: ${replay_cost:,.4f}")
    print()
    print("Capsule")
    print(f"  cumulative delta tokens: {format_int(total_capsule_tokens)}")
    print(f"  synthetic prefill units: {format_int(total_capsule_prefill)}")
    print(f"  estimated input cost: ${capsule_cost:,.4f}")
    print()
    print("Reduction")
    print(f"  token replay reduction: {token_reduction:.2%}")
    print(f"  synthetic prefill reduction: {prefill_reduction:.2%}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare naive transcript replay against session capsule resumption."
    )
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--initial-context", type=int, default=4000)
    parser.add_argument("--growth-per-step", type=int, default=1000)
    parser.add_argument("--output-tokens", type=int, default=300)
    parser.add_argument(
        "--restore-overhead-tokens",
        type=int,
        default=200,
        help="Proxy for restore/decode overhead expressed in token-equivalent units.",
    )
    parser.add_argument(
        "--input-price-per-m",
        type=float,
        default=1.00,
        help="Input token price per million for rough scenario costing.",
    )
    parser.add_argument("--csv", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics = build_metrics(
        steps=args.steps,
        initial_context=args.initial_context,
        growth_per_step=args.growth_per_step,
        output_tokens=args.output_tokens,
        restore_overhead_tokens=args.restore_overhead_tokens,
    )
    if args.csv:
        write_csv(args.csv, metrics)
    print_summary(metrics, args.input_price_per_m)


if __name__ == "__main__":
    main()

