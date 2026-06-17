#!/usr/bin/env python3
"""Run the repository's dependency-free smoke checks."""

from __future__ import annotations

import argparse
import py_compile
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PYTHON_FILES = [
    ROOT / "scripts" / "benchmark_llama_capsules.py",
    ROOT / "scripts" / "capsule_cli.py",
    ROOT / "scripts" / "capsule_gateway.py",
    ROOT / "scripts" / "simulate_capsules.py",
    ROOT / "scripts" / "test_capsule_cli_help.py",
    ROOT / "scripts" / "test_capsule_cli_export_import.py",
    ROOT / "scripts" / "test_capsule_cli_fake_llamacpp.py",
    ROOT / "scripts" / "test_capsule_cli_storage_gc.py",
    ROOT / "scripts" / "test_capsule_cli_model_plane_jobs.py",
    ROOT / "scripts" / "test_capsule_gateway_fake_backend.py",
    ROOT / "scripts" / "validate_schema_examples.py",
]

TEST_SCRIPTS = [
    ROOT / "scripts" / "validate_schema_examples.py",
    ROOT / "scripts" / "test_capsule_cli_help.py",
    ROOT / "scripts" / "test_capsule_cli_fake_llamacpp.py",
    ROOT / "scripts" / "test_capsule_cli_export_import.py",
    ROOT / "scripts" / "test_capsule_cli_storage_gc.py",
    ROOT / "scripts" / "test_capsule_cli_model_plane_jobs.py",
    ROOT / "scripts" / "test_capsule_gateway_fake_backend.py",
]

ASCII_SUFFIXES = {".md", ".py", ".json", ".jsonc", ".example", ".ps1"}
SKIP_PARTS = {".git", "__pycache__", ".capsules"}
UTF8_BOM = b"\xef\xbb\xbf"


def print_step(label: str) -> None:
    print(f"\n== {label} ==")


def should_scan_ascii(path: Path) -> bool:
    if any(part in SKIP_PARTS for part in path.parts):
        return False
    if "data" in path.parts and "runs" in path.parts:
        return False
    return path.suffix in ASCII_SUFFIXES or path.name.endswith(".env.example")


def check_ascii() -> None:
    failures: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or not should_scan_ascii(path):
            continue
        data = path.read_bytes()
        start = len(UTF8_BOM) if data.startswith(UTF8_BOM) else 0
        for offset, byte in enumerate(data[start:], start=start):
            if byte > 0x7F:
                rel = path.relative_to(ROOT).as_posix()
                failures.append(f"{rel}: non-ASCII byte 0x{byte:02x} at offset {offset}")
                break
    if failures:
        raise RuntimeError("ASCII check failed:\n" + "\n".join(failures))
    print("ascii ok")


def compile_python() -> None:
    for path in PYTHON_FILES:
        py_compile.compile(str(path), doraise=True)
    print(f"compiled {len(PYTHON_FILES)} files")


def run_script(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    print(f"$ {sys.executable} {rel}")
    result = subprocess.run(
        [sys.executable, str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"{rel} failed with exit code {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Session Capsules smoke checks.")
    parser.add_argument("--compile-only", action="store_true", help="Only compile Python files and run the ASCII check.")
    args = parser.parse_args()

    try:
        print_step("Python compile")
        compile_python()
        print_step("ASCII")
        check_ascii()
        if not args.compile_only:
            print_step("Smoke tests")
            for script in TEST_SCRIPTS:
                run_script(script)
        print("\nall smoke checks passed")
        return 0
    except Exception as exc:  # noqa: BLE001 - runner reports a concise failure.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
