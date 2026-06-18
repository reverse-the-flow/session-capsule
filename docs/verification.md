# Verification

## Smoke Test Command

The repo has one dependency-free verification command:

```powershell
py -3 .\scripts\run_smoke_tests.py
```

It performs:

- Python syntax compilation
- ASCII scan for repo text artifacts
- schema/example validation, including restore-failure schema fields
- Model Plane gateway launch-profile schema/example validation
- conceptual CLI help smoke test
- state-location help and `state info` smoke test
- fake `llama.cpp` hard capsule save/restore, shutdown job, and failed-restore fallback smoke test
- store-relative hard snapshot reference smoke test
- `.scap` export/import/verify, dry-run sizing, endpoint compatibility warning, signature, and tamper-rejection smoke test
- storage config, pinning, stats, and GC smoke test
- Model Plane job-packet smoke test, including launch-profile rendering/checking, shutdown planning, signed export jobs, and authenticated gateway transport job packets
- local gateway fake-backend smoke test
- gateway auth plus signed bundle export/list/download/upload/delete smoke path
- gateway status discovery for transport API version, upload size, content type, auth policy, signing policy, endpoint paths, and bundle capabilities
- gateway status discovery for identity headers and Open WebUI/opencode metadata mappings

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
- transport status discovery for Model Plane upload/download integration
- identity status discovery for Open WebUI and opencode thread metadata
- store-relative snapshot refs in hard checkpoint manifests
- `.scap` export, list, download, raw upload import, and bundle delete
- authenticated gateway requests and transport job packets when a token is configured
- Model Plane gateway launch-profile command rendering, authenticated status checking, and inline secret-value rejection

Live client verification remains an operator step:

- point Open WebUI at `http://host.docker.internal:8765/v1` when Open WebUI runs in Docker
- point host-native clients at `http://127.0.0.1:8765/v1`
- disable streaming for the gateway v0
- supply stable thread headers where the client supports them
