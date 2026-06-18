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
- opencode integration config generation smoke test
- state-location help and `state info` smoke test
- fake `llama.cpp` endpoint doctor slot probe, endpoint compatibility matrix, hard capsule save/restore, shutdown job, and failed-restore fallback smoke test
- state-relative ledger, prefill, and hard snapshot reference smoke test
- `.scap` export/import/verify, dry-run sizing, endpoint compatibility warning, signature, sealed envelope with recipient-file key reference, and tamper-rejection smoke test
- `.scap` import thread-id override and ref remapping smoke test
- `.scap` redacted export/import smoke test for metadata-only transcript sharing
- storage config, pinning, stats, and GC smoke test
- Model Plane job-packet smoke test, including launch-profile rendering/checking, required transport capability gating, shutdown planning, signed export jobs, and authenticated gateway transport job packets
- local gateway fake-backend smoke test
- gateway auth plus signed bundle export/list/download/store/upload/delete smoke path
- gateway status discovery for transport API version, upload size, content type, auth policy, signing policy, endpoint paths, and bundle capabilities
- gateway status discovery for endpoint compatibility and hard checkpoint readiness
- gateway status discovery for identity headers and Open WebUI/opencode metadata mappings

## Current Evidence

Verified on 2026-06-18:

```text
schema examples ok
CLI conceptual help smoke test ok
opencode integration config generation smoke test ok
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
- persisted `endpoint doctor` slot probe evidence from `/slots`
- endpoint compatibility matrix summarizing persisted slot probe evidence for launchers
- explicit `X-Capsule-*` headers
- Open WebUI-style forwarded identity headers
- hard restore plus diff forwarding
- checkpoint after response
- transport status discovery for Model Plane upload/download integration
- launch-profile required capability verification before enabling Model Plane upload/download controls
- endpoint compatibility status discovery for Model Plane hard checkpoint gating
- gateway CORS preflight and exposed download headers for browser-hosted upload/download controls
- identity status discovery for Open WebUI and opencode thread metadata
- state-relative ledger, prefill, and snapshot refs in runtime-written files
- `.scap` export, list, download, store-only upload, raw upload import, and bundle delete
- gateway redacted bundle export
- gateway raw-upload and stored-bundle import target-thread override
- gateway store-only upload without thread-state import
- authenticated gateway requests and transport job packets when a token is configured
- Model Plane gateway launch-profile command rendering, authenticated status checking, required capability rejection, endpoint readiness reporting, and inline secret-value rejection

Live client verification remains an operator step:

- point Open WebUI at `http://host.docker.internal:8765/v1` when Open WebUI runs in Docker
- point host-native clients at `http://127.0.0.1:8765/v1`
- disable streaming for the gateway v0
- supply stable thread headers where the client supports them
