# Verification

## Smoke Test Command

The repo has one dependency-free verification command:

```powershell
py -3 .\scripts\run_smoke_tests.py
```

It performs:

- Python syntax compilation
- ASCII scan for repo text artifacts
- schema/example validation
- conceptual CLI help smoke test
- fake `llama.cpp` hard capsule save/restore smoke test
- `.scap` export/import/verify and tamper-rejection smoke test
- storage config, pinning, stats, and GC smoke test
- Model Plane job-packet smoke test, including gateway transport job packets
- local gateway fake-backend smoke test
- gateway bundle export/list/download/upload/delete smoke path

## Current Evidence

Verified on 2026-06-18:

```text
schema examples ok
CLI conceptual help smoke test ok
fake llama.cpp CLI smoke test ok
.scap export/import smoke test ok
storage config and GC smoke test ok
model-plane job packet smoke test ok
capsule gateway fake backend smoke test ok
```

## External Verification

The smoke suite intentionally does not mutate a user's live Open WebUI or opencode configuration.

The gateway integration contract is verified by fake-backend tests that exercise:

- OpenAI-compatible `/v1/chat/completions`
- explicit `X-Capsule-*` headers
- Open WebUI-style forwarded identity headers
- hard restore plus diff forwarding
- checkpoint after response
- `.scap` export, list, download, raw upload import, and bundle delete

Live client verification remains an operator step:

- point Open WebUI at `http://host.docker.internal:8765/v1` when Open WebUI runs in Docker
- point host-native clients at `http://127.0.0.1:8765/v1`
- disable streaming for the gateway v0
- supply stable thread headers where the client supports them
