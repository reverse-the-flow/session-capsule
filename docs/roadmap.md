# Session Capsules Roadmap

## Direction

The first public version should be a standalone capsule harness, not a general server platform and not a passive watcher. The missing layer belongs in the request path:

```text
client or CLI
  -> capsule harness or gateway
    -> model endpoint adapter
      -> runtime slots and KV state
```

This keeps the project uploadable by itself while leaving a clean path for later Open WebUI, opencode, and Model Plane integration.

## Design Rules

1. The thread transcript is canonical.
2. The capsule is acceleration.
3. The slot is temporary runtime placement.
4. Model weights stay with the runtime and are never transported as capsules.
5. Every hard capsule is bound to a model, tokenizer, runtime build, context limit, and slot format.
6. Every restore path must have a transcript replay fallback.
7. Start with explicit CLI commands before adding background behavior.
8. Integrate in the request path before considering any watcher.

## Stage 0: Uploadable Repo Baseline

Goal: Make the repo understandable and runnable without private chat context.

Implementation steps:

- Keep `README.md` focused on what the project is, what it is not, and the first runnable path.
- Add this roadmap and keep `docs/mvp-plan.md` focused on the benchmark harness.
- Add a short `docs/protocol.md` once the first manifest fields stabilize.
- Keep sample data small and inspectable.
- Ensure generated capsule files and run folders stay ignored by git.
- Make the first commit before expanding integrations.

Exit criteria:

- A new reader can identify the concept, the local `llama.cpp` path, and the next command to run.
- The repo can be shared without raw capsule blobs, model weights, or machine-specific secrets.

## Stage 1: Manifest And Ledger Schema

Goal: Define the stable data model before adding more code.

Files to add:

```text
schemas/capsule-manifest.schema.json
schemas/thread-ledger.schema.json
schemas/endpoint-capabilities.schema.json
examples/thread-ledger.example.json
examples/capsule-manifest.example.json
examples/endpoint-capabilities.example.json
scripts/validate_schema_examples.py
```

Core objects:

```json
{
  "capsule_id": "cap_004",
  "thread_id": "thread_abc",
  "kind": "thread_checkpoint",
  "parent_capsule_id": "cap_003",
  "endpoint_id": "local-llamacpp",
  "model_hash": "sha256-...",
  "tokenizer_hash": "sha256-...",
  "runtime": "llama.cpp-build-id",
  "context_start": 0,
  "context_end": 12480,
  "snapshot_ref": "threads/thread_abc/snapshots/cap_004.bin",
  "token_digest": "sha256-..."
}
```

Implementation steps:

- Separate `thread-ledger.json` from `capsule-manifest.json`.
- Track token spans, parent capsule, endpoint id, and compatibility fingerprints.
- Track transcript diffs after the last capsule checkpoint.
- Define capability flags:
  - `soft_capsules`
  - `server_side_handles`
  - `slot_save_restore`
  - `user_carried_blobs`
  - `sealed_blobs`
- Add schema validation to the CLI once the CLI exists.

Exit criteria:

- The thread ledger can answer: latest compatible capsule, diff after checkpoint, fallback replay range.
- The capsule manifest can answer: can this endpoint restore this snapshot?

Initial status:

- `schemas/thread-ledger.schema.json` allows capsule links to become `missing` or `restore_failed`.
- `schemas/capsule-manifest.schema.json` records snapshot lifecycle and last restore failure metadata.
- `scripts/validate_schema_examples.py` checks the schema files include restore-failure fields.

## Stage 2: CLI Soft Capsules

Goal: Build useful thread bookkeeping before depending on hard KV restore.

Command shape:

```powershell
capsule endpoint add local-llamacpp --type llamacpp --base-url http://localhost:8080
capsule endpoint doctor local-llamacpp
capsule thread start --endpoint local-llamacpp --name test-thread
capsule thread append --thread test-thread --message delta.json
capsule checkpoint --thread test-thread --soft
capsule inspect --thread test-thread
```

Implementation steps:

- Add a small Python CLI, probably `scripts/capsule_cli.py` first.
- Store local state under `.capsules/` or a configurable data directory.
- Record transcripts as JSONL.
- Record thread ledgers and soft checkpoint manifests.
- Implement `doctor` without requiring a model load.
- Implement `inspect` to show the active thread, endpoint, latest capsule, and fallback status.

Exit criteria:

- The CLI can manage threads and checkpoints even against endpoints that do not expose KV save/restore.
- Hosted or incompatible endpoints degrade to transcript diff/replay rather than failing the workflow.

## Stage 3: Local Hard Capsules With llama.cpp

Goal: Prove the true capsule primitive on a local endpoint.

Runtime assumptions:

- `llama.cpp` server exposes `/slots`.
- The server supports slot save and restore.
- The user starts the model endpoint separately.
- The harness controls only requests, manifests, and saved artifacts.

Command shape:

```powershell
capsule endpoint doctor local-llamacpp
capsule prefill create --endpoint local-llamacpp --name user_default --input user_prefill.md
capsule thread start --endpoint local-llamacpp --prefill user_default --name experiment-a
capsule checkpoint --thread experiment-a
capsule resume --thread experiment-a
capsule shutdown --thread experiment-a
```

Implementation steps:

- Add an endpoint adapter interface:
  - `capabilities()`
  - `allocate_slot()`
  - `restore(capsule, slot)`
  - `append(slot, delta)`
  - `generate(slot, params)`
  - `save(slot, capsule_ref)`
- Implement the `llama.cpp` adapter using slot endpoints.
- On thread reload:
  1. load the thread ledger
  2. choose the latest compatible capsule
  3. restore it into a slot
  4. append the transcript diff after `context_end`
  5. save a new checkpoint after the response
- Add dirty-slot tracking so `shutdown` saves before model unload.
- Keep raw `.bin` snapshots local and ignored by git.

Exit criteria:

- A thread can be started, checkpointed, resumed, appended, and checkpointed again.
- If restore fails, the CLI replays the transcript and writes a new compatible checkpoint.

## Stage 4: User Prefill Capsules

Goal: Make stable user or project context reusable as a root checkpoint.

Implementation steps:

- Support named prefill capsules:
  - `user_default`
  - `project_default`
  - `repo_map`
- Treat prefill capsules as roots or early parents in the capsule chain.
- Version prefill capsules instead of patching the middle of a token sequence.
- Store the source text used to create the prefill for audit and fallback.
- Add `capsule prefill diff` to compare source changes before compiling a new prefill.

Exit criteria:

- Starting a new thread can restore a user or project prefill before appending the first live message.
- A prefill source change creates a new version rather than mutating prior thread history.

Initial status:

- `prefill create` supports soft source-only prefills and hard local `llama.cpp` slot prefills.
- `prefill list` shows available prefill names and versions.
- `prefill diff` compares a new source file or string against the active prefill version.
- `thread start --prefill NAME` attaches the active prefill as the root capsule.
- The fake `llama.cpp` test verifies first live message tokens begin after the prefill and hard checkpoints preserve the parent prefill segment.

## Stage 5: Exportable `.scap` Bundles

Goal: Make a thread portable as an artifact without pretending KV state is universally portable.

Bundle shape:

```text
thread.scap
  manifest.json
  thread-ledger.json
  transcript.jsonl
  capsule-index.json
  snapshots/
    cap_001.bin optional
    cap_002.bin optional
```

Implementation steps:

- Implement `capsule export --thread X --out X.scap`.
- Implement `capsule import X.scap`.
- Allow export modes:
  - `ledger-only`
  - `with-local-snapshots`
  - `with-redacted-transcript`
- Add size reporting before export.
- Add compatibility warnings on import.

Exit criteria:

- A user can export a thread ledger and transcript without moving model weights.
- Snapshot blobs can be included for same-runtime restore or omitted for safe sharing.

Initial status:

- `export --thread X --out X.scap` writes a zip bundle with ledger, transcript, endpoint metadata, capsule manifests, prefill sources, and capsule index.
- Snapshot blobs are omitted by default and require `--include-snapshots`.
- `export --dry-run` prints the planned bundle entries and estimated payload bytes before writing.
- `export --redact-transcript` writes a metadata-only bundle without transcript or prefill source text and marks replay fallback unavailable.
- `import X.scap` restores endpoint, prefill, and thread files into a fresh state directory.
- `import X.scap --thread-id NEW_ID` remaps thread-owned ledger, transcript, manifest, and snapshot refs under `threads/NEW_ID/`.
- `import X.scap` warns when the bundle endpoint id already exists locally with different runtime, model, tokenizer, context, slot, or URL metadata.
- `scripts/test_capsule_cli_export_import.py` validates a ledger-only bundle round trip, dry-run sizing, and endpoint compatibility warnings.

## Stage 6: Local Capsule Gateway

Goal: Let non-terminal apps use capsules by routing through a local request-path layer.

Preferred shape:

```text
Open WebUI or opencode
  -> http://localhost:8765/v1/chat/completions
    -> capsule gateway
      -> local model endpoint
```

Implementation steps:

- Build a small OpenAI-compatible proxy after the CLI contract is stable.
- The gateway should accept normal chat completion requests.
- Add optional headers:
  - `X-Capsule-Thread`
  - `X-Capsule-Workspace`
  - `X-Capsule-Prefill`
- If headers are absent, use a conservative generated thread id rather than guessing aggressively.
- Gateway flow:
  1. identify thread
  2. restore latest compatible capsule
  3. append request delta
  4. forward generation to backend
  5. checkpoint response
  6. return normal OpenAI-compatible response
- Expose status endpoints:
  - `/api/capsules/status`
  - `/api/capsules/threads`
  - `/api/capsules/checkpoint`

Exit criteria:

- Open WebUI or opencode can point at the gateway as a custom endpoint.
- The gateway creates and updates thread ledgers without browser scraping or log watching.

Initial status:

- `scripts/capsule_gateway.py` exposes `/v1/chat/completions` as a local OpenAI-compatible proxy.
- The gateway accepts `X-Capsule-Thread`, `X-Capsule-Workspace`, and `X-Capsule-Prefill`.
- In soft mode, it records the canonical transcript and checkpoints metadata after each response.
- In hard mode, it restores the latest compatible local snapshot into a configured slot, forwards only the transcript diff, and saves a new checkpoint after the response.
- It exposes `/api/capsules/status`, `/api/capsules/threads`, and `/api/capsules/checkpoint`.
- `scripts/test_capsule_gateway_fake_backend.py` validates the request-path gateway flow against a fake OpenAI/slot backend.

## Stage 7: Native App Integrations

Goal: Improve thread identity and UX without making the core app-specific.

Implementation steps:

- Add an opencode integration that passes workspace and thread identifiers explicitly.
- Add an Open WebUI integration only after the gateway works as a plain endpoint.
- Prefer headers or explicit metadata over passive file watching.
- Keep each integration thin:
  - send thread id
  - send workspace id
  - request checkpoint or resume
  - display capsule status

Exit criteria:

- App integrations improve metadata and controls but are not required for the core workflow.

Initial status:

- `docs/integrations.md` defines the thin-integration rule: clients point at the gateway and pass explicit metadata when possible.
- `examples/integrations/open-webui.env.example` points Open WebUI at the gateway and enables forwarded chat/user headers.
- `examples/integrations/opencode.capsule-provider.jsonc` defines a custom OpenAI-compatible provider with capsule headers supplied from environment variables.
- The gateway maps `X-OpenWebUI-Chat-Id` and `X-OpenWebUI-User-Id` into thread/workspace metadata.
- The gateway also accepts `X-Opencode-Thread`, `X-Opencode-Session`, and `X-Opencode-Workspace` for future native opencode hooks.

## Stage 8: Model Plane Integration

Goal: Let Model Plane coordinate capsule-capable work after the standalone primitive is proven.

Model Plane should own:

- endpoint registry
- endpoint capability cache
- thread and capsule registry
- job packets
- policy and fallback routing

Model Plane should not own:

- model weights
- live KV tensors
- runtime slot layout
- the primary inference loop

Implementation steps:

- Make Model Plane consume the same thread ledger and endpoint capability schemas.
- Add job packet types:
  - `resume_thread`
  - `checkpoint_thread`
  - `shutdown_thread`
  - `export_thread`
  - `validate_capsule`
- Let external schedulers or skills launch the harness from Model Plane job packets.
- Keep cleanup explicit: save dirty checkpoint, update ledger, release slot, then unload runtime if needed.

Exit criteria:

- Model Plane can route capsule-aware jobs without becoming the inference backend.
- The standalone CLI/gateway still works without Model Plane.

Initial status:

- `schemas/model-plane-job.schema.json` defines the first job-packet contract.
- `examples/model-plane/` contains example packets for `resume_thread`, `checkpoint_thread`, `shutdown_thread`, `export_thread`, and `validate_capsule`.
- `capsule_cli.py job run JOB.json` executes those packets through the existing harness paths.
- `--dry-run` prints packet intent without touching the ledger or runtime.
- `shutdown_thread` lets Model Plane ask the harness to save a dirty checkpoint before runtime unload.
- `export_thread` jobs can sign bundles with runner-side `--signature-key-file`, `--signature-key-env`, and `--signature-key-id` flags.
- Job packet validation rejects secret key or auth-token params so secrets stay outside packets.
- `docs/model-plane.md` records the boundary: Model Plane owns routing and policy, not model weights, live KV tensors, runtime slot layout, or the inference loop.
- Gateway transport job packet types cover bundle export, list, download, import, and delete through the local gateway API.

## Development Order

Recommended order:

1. Stage 0: uploadable repo baseline
2. Stage 1: schema and examples
3. Stage 2: CLI soft capsules
4. Stage 3: local hard capsules
5. Stage 4: user prefill capsules
6. Stage 5: `.scap` export/import
7. Stage 6: local gateway
8. Stage 7: native integrations
9. Stage 8: Model Plane
10. Stage 9: capsule storage management
11. Stage 10: gateway bundle transport
12. Stage 11: bundle integrity, signing, and sealing
13. Stage 12: gateway access control
14. Stage 13: Model Plane gateway launch profile
15. Stage 14: state reference policy
16. Stage 15: state location policy
17. Stage 16: gateway identity contract

Do not start with gateway or Model Plane. They become simpler after the ledger, manifest, and CLI lifecycle are real.

## Stage 9: Capsule Storage Management

Goal: Treat hard capsule snapshots as managed cache artifacts instead of letting them grow without bound.

Implementation steps:

- Add persistent settings under `.capsules/config/settings.json`.
- Add a storage budget with a sensible default.
- Add a disk free-space floor.
- Add pin/unpin for important thread capsules.
- Add stats and GC commands.
- Keep transcripts, ledgers, manifests, and soft checkpoints durable.
- Delete only eligible hard snapshot blobs.
- Mark manifests or ledger links when a hard snapshot blob is missing.

Initial status:

- `config init/show/set` manages persistent settings.
- Default storage budget is `50GB` with `20GB` minimum free disk.
- `stats` reports snapshot bytes, reclaimable bytes, protected snapshots, quota, and free disk.
- `pin` and `unpin` protect specific thread capsules.
- `gc --dry-run` previews oldest-unpinned-first cleanup.
- `gc --apply` deletes eligible hard snapshot blobs and marks their ledger links as `missing`.
- `schemas/capsule-config.schema.json` and `examples/capsule-config.example.json` define the first config contract.
- `scripts/test_capsule_cli_storage_gc.py` validates config, pinning, latest-per-thread protection, and GC behavior.

## Stage 10: Gateway Bundle Transport

Goal: Let Model Plane or a local UI upload/download `.scap` bundles through the gateway without reimplementing export/import mechanics.

Implementation steps:

- Add a gateway export endpoint backed by the existing CLI export path.
- Store gateway-created bundles under `.capsules/bundles/`.
- Add bundle listing and download endpoints.
- Add raw `.scap` upload and import.
- Add import-by-existing-bundle for local control-plane workflows.
- Add a stored-bundle delete endpoint for cleanup after transfer.
- Keep export ledger-only by default.
- Require explicit opt-in before including hard local snapshots.
- Enforce a gateway upload-size limit.
- Keep transport local/control-plane oriented; Model Plane owns auth, UI, remote exposure, TTL, audit, and policy.

Exit criteria:

- A local UI can export a thread and download the resulting `.scap`.
- A local UI can upload a `.scap` and import it into the gateway state.
- Model Plane can call the gateway for capsule transport without becoming the capsule archive format implementation.

Initial status:

- `POST /api/capsules/export` creates a `.scap` bundle.
- `GET /api/capsules/bundles` lists stored local bundles.
- `GET /api/capsules/bundles/{bundle_id}` downloads a stored bundle with capsule-specific headers.
- `POST /api/capsules/import` imports either an existing stored bundle or raw uploaded `.scap` bytes.
- Gateway import accepts a target thread id through JSON `thread_id` or raw-upload `X-Capsule-Import-Thread`.
- `DELETE /api/capsules/bundles/{bundle_id}` deletes a stored bundle without deleting imported thread state.
- `/api/capsules/status` advertises a versioned `transport` object with endpoint paths, upload size, content type, auth policy, signing policy, and enabled bundle capabilities for Model Plane discovery.
- Bundle ids are slugged and scoped to `.capsules/bundles/`.
- The gateway launch flag `--max-bundle-bytes` caps raw upload size.
- Gateway launch flags can sign exported bundles and require verified signatures before import.
- The gateway launch flag `--cors-allow-origin` enables browser preflight for direct Model Plane upload/download controls.
- `/api/capsules/status` advertises `transport.cors` so Model Plane can discover whether browser transfer is enabled for this gateway instance.
- `scripts/test_capsule_gateway_fake_backend.py` validates export, list, download, raw upload import, and delete through the gateway.
- Model Plane job packets can now invoke the gateway transport endpoints through `capsule_cli.py job run`.
- `gateway_import_bundle` job packets can pass `params.thread_id` as the target local import thread id.
- Protected gateway transport jobs authenticate through runner-side `--gateway-auth-token-file` or `--gateway-auth-token-env` flags.
- Direct CLI gateway client commands now call the same transport API: `gateway status`, `gateway list`, `gateway export`, `gateway download`, `gateway upload`, `gateway import`, and `gateway delete`.
- `scripts/test_capsule_cli_model_plane_jobs.py` validates protected direct CLI upload/download controls against the in-process gateway alongside Model Plane job packets.

## Stage 11: Bundle Integrity, Signing, And Sealing

Goal: Make transported bundles verifiable now, then add authenticity and confidentiality without blocking the local MVP.

Implementation steps:

- Add per-entry digest metadata to `.scap` bundle manifests.
- Add a CLI verification command for bundle integrity.
- Verify digest-indexed bundles before import extraction.
- Reject duplicate zip entries.
- Keep digest verification separate from cryptographic signing.
- Add a signature envelope for shared-key authenticity.
- Add a later encryption or sealed-blob envelope for user-carried capsules.
- Keep model weights outside the capsule envelope.

Exit criteria:

- A user can verify that a `.scap` bundle's entries match the exported manifest.
- Import fails before extraction if a digest-indexed bundle is corrupted or contains duplicate entries.
- A user can sign a `.scap` bundle with an explicit local key source.
- The roadmap clearly distinguishes implemented integrity/signing from future encryption.

Initial status:

- New `.scap` exports include `integrity.file_digest_algorithm = sha256`.
- New `.scap` exports include `file_digests` for every zip entry except `manifest.json`.
- Redacted `.scap` exports omit transcript and prefill source text, mark `transcript_redacted=true`, and preserve that warning on import.
- `capsule_cli.py verify BUNDLE.scap` verifies the file digest index.
- `export --signature-key-file KEY --signature-key-id ID` writes an optional HMAC-SHA256 signature.
- `capsule_cli.py job run EXPORT_JOB.json --signature-key-file KEY` signs direct Model Plane export jobs without storing the key in the packet.
- `verify --signature-key-file KEY --require-signature` verifies the bundle signature.
- `import --signature-key-file KEY --require-signature` verifies a required signature before extraction.
- `inspect --bundle BUNDLE.scap` reports plaintext content, redaction, snapshots, signing, encryption status, and trusted-transport policy.
- Gateway bundle listings expose `share_safety`, `trusted_transport_required`, `transcript_included`, `prefill_sources_included`, and signing/encryption metadata.
- `capsule_gateway.py --signature-key-file KEY --require-bundle-signature` applies signing and required verification to gateway transport.
- `import BUNDLE.scap` verifies bundles that include `file_digests` before extracting state files.
- `scripts/test_capsule_cli_export_import.py` validates successful verification, signature checks, and tamper rejection.
- Encryption and sealed user-carried blobs are not implemented yet; redaction is metadata-only and not cryptographic sealing.

## Stage 12: Gateway Access Control

Goal: Keep upload/download and request-path gateway surfaces local by default, with an explicit token gate before wider exposure.

Implementation steps:

- Keep default gateway binding on `127.0.0.1`.
- Add optional request-token authentication for all gateway routes.
- Accept standard `Authorization: Bearer TOKEN` for OpenAI-compatible clients.
- Accept `X-Capsule-Gateway-Key` for local control scripts.
- Keep auth tokens out of persistent capsule config, endpoint records, and job packets.
- Report whether auth is active from the status endpoint without exposing the token.
- Smoke-test unauthenticated rejection and authenticated gateway transport.

Exit criteria:

- A gateway launched with an auth token rejects unauthenticated requests.
- Existing OpenAI-compatible clients can use their API key field as the bearer token.
- Local UI/control clients have a simple explicit header option.

Initial status:

- `capsule_gateway.py --auth-token-file TOKENFILE` and `--auth-token-env ENVNAME` enable request-token authentication.
- Authenticated requests may use `Authorization: Bearer TOKEN` or `X-Capsule-Gateway-Key: TOKEN`.
- `capsule_cli.py job run` can call protected gateway transport jobs with `--gateway-auth-token-file TOKENFILE` or `--gateway-auth-token-env ENVNAME`.
- `/api/capsules/status` reports `auth_required` without exposing the token.
- `scripts/test_capsule_gateway_fake_backend.py` verifies unauthenticated rejection and authenticated signed transport.

## Stage 13: Model Plane Gateway Launch Profile

Goal: Let Model Plane launch the capsule gateway from a small, versioned profile instead of hardcoding a shell command or absorbing gateway internals.

Implementation steps:

- Add a gateway launch-profile schema.
- Add an example local `llama.cpp` gateway launch profile.
- Keep tokens and signing keys out of the profile; allow only secret references.
- Map profile values directly to gateway launch flags.
- Include an optional exact browser origin for Model Plane upload/download controls.
- Require Model Plane to read `/api/capsules/status` after launch and verify the status `transport` object before enabling upload/download controls.
- Validate the profile in the repo smoke suite.

Exit criteria:

- Model Plane can read a profile, start the gateway, and know which URL to health-check.
- The profile defines gateway state directory, endpoint, host, port, checkpoint mode, slot, timeout, upload cap, auth reference, and bundle-signing reference.
- Browser-hosted controls can opt into gateway CORS without changing the bundle protocol.
- The profile does not contain gateway tokens or signing key values.

Initial status:

- `schemas/model-plane-gateway-launch.schema.json` defines the first launch-profile contract.
- `examples/model-plane/gateway-launch-profile.example.json` shows a local `llama.cpp` hard-checkpoint gateway profile.
- `capsule_cli.py gateway command PROFILE --json` renders a launch profile into concrete `capsule_gateway.py` arguments plus the OpenAI base URL and status URL.
- `capsule_cli.py gateway check PROFILE --json` calls the profile status URL, authenticates from the profile's request-auth reference, and verifies the live gateway status/transport contract.
- `gateway.cors_allow_origin` maps to `--cors-allow-origin` and is checked against `transport.cors` when present.
- `scripts/validate_schema_examples.py` validates launch profiles separately from job packets.
- `scripts/test_capsule_cli_model_plane_jobs.py` verifies launch-profile command rendering, authenticated status checking, and inline secret-value rejection.
- `docs/model-plane.md` and `docs/configuration.md` explain how Model Plane maps the profile to gateway launch flags and status discovery.

## Stage 14: State Reference Policy

Goal: Make local ledger, manifest, prefill source, and snapshot references portable inside a capsule state directory while preserving runtime-visible filenames needed by slot APIs.

Implementation steps:

- Use state-relative refs for `thread.transcript_ref`, `thread.capsules[].manifest_ref`, `prefill_source.source_ref`, and `storage.snapshot_ref`.
- Keep `.capsules/` out of ledger and manifest refs because it is only the default root, not part of the portable reference.
- Keep runtime-visible filenames in `storage.runtime_snapshot_ref`.
- Keep `storage.snapshot_digest` as metadata instead of making v0 paths content-addressed.
- Validate examples and smoke-test runtime-written ledgers/manifests for the policy.

Exit criteria:

- Newly written ledgers and manifests use state-relative refs.
- Examples reject absolute, escaping, or `.capsules/`-prefixed state refs.
- The docs clearly distinguish state refs, runtime paths, and content digests.

Initial status:

- `Store.relative_ref()` writes runtime-created snapshot refs relative to the capsule state directory.
- `examples/thread-ledger.example.json`, `examples/capsule-manifest.example.json`, and `examples/prefill-manifest.example.json` use refs without the `.capsules/` prefix.
- `scripts/validate_schema_examples.py` rejects absolute, escaping, or `.capsules/`-prefixed state refs.
- `scripts/test_capsule_cli_fake_llamacpp.py` verifies runtime-written ledger, prefill, and snapshot refs are state-relative.
- Import thread-id override uses the state-reference policy to remap thread-owned files while keeping prefill and endpoint records state-global.
- `docs/protocol.md` and `docs/configuration.md` document the v0 state-reference policy.

## Stage 15: State Location Policy

Goal: Keep v0 state simple and project-local while preserving an explicit override path for tests, shared workspaces, and Model Plane launch profiles.

Implementation steps:

- Make `.capsules/` the default and recommended project-local state root.
- Keep `--state-dir` as the explicit override.
- Treat user-level/global state as a future integration option, not the v0 default.
- Add a CLI command that reports the active state root and policy.
- Document where persistent config, endpoint records, threads, bundles, and hard snapshots live relative to the selected state root.

Exit criteria:

- Users can inspect which state root a command will use.
- The docs answer whether state is project-local or user-level in v0.
- The roadmap no longer carries state location as an unresolved open question.

Initial status:

- `capsule_cli.py state info` reports the selected state directory, config path, endpoint/thread counts, default state root, and override flag.
- `capsule help state` documents the project-local default and `--state-dir` override.
- `docs/configuration.md` and `docs/help.md` state that `.capsules/` is the v0 default and user-level/global state is future work.
- `scripts/test_capsule_cli_help.py` covers the state help topic and `state info` output.

## Stage 16: Gateway Identity Contract

Goal: Make thread identity metadata discoverable so Open WebUI, opencode, Model Plane, and local UIs can bind requests to stable capsule threads without client-specific guessing.

Implementation steps:

- Publish preferred identity headers from `/api/capsules/status`.
- Publish accepted client-native thread and workspace headers.
- Define the minimum useful metadata for generic OpenAI-compatible clients, Open WebUI, and opencode.
- Keep generated thread ids as best-effort fallback only.
- Test identity discovery through the fake gateway smoke path.

Exit criteria:

- A client can discover which header gives durable thread continuity.
- The docs answer the smallest useful thread id metadata for Open WebUI and opencode.
- The gateway status payload and integration docs agree.

Initial status:

- `/api/capsules/status` includes an `identity` object with preferred headers, accepted headers, client mappings, fallback behavior, default thread prefix, and default prefill.
- Generic clients need `X-Capsule-Thread`.
- Open WebUI needs `X-OpenWebUI-Chat-Id`; `X-OpenWebUI-User-Id` is optional workspace metadata.
- opencode needs `X-Opencode-Thread` or `X-Opencode-Session`; `X-Opencode-Workspace` is optional workspace metadata.
- `scripts/test_capsule_gateway_fake_backend.py` verifies identity contract discovery.
- `docs/integrations.md`, `docs/protocol.md`, and `docs/transport.md` document the contract.

## First Three Implementation Tickets

### Ticket 1: Schema Pack

- Add `schemas/`.
- Add capsule manifest, thread ledger, and endpoint capability schemas.
- Add examples that match the current benchmark scenario.
- Add a tiny validation command or script.

Initial status:

- `schemas/` exists with capsule manifest, thread ledger, and endpoint capability schemas.
- `examples/` exists with matching example documents.
- `scripts/validate_schema_examples.py` performs dependency-free invariant checks.

### Ticket 2: CLI Ledger MVP

- Add `scripts/capsule_cli.py`.
- Implement:
  - `endpoint add`
  - `endpoint doctor`
  - `thread start`
  - `thread append`
  - `checkpoint --soft`
  - `inspect`
- Store state in `.capsules/`.

Initial status:

- `scripts/capsule_cli.py` implements endpoint records, thread start, message append, soft checkpoint, endpoint doctor, and inspect.
- The command stores local state under `.capsules/` by default.
- Hard restore is intentionally not implemented in this ticket.

### Ticket 3: llama.cpp Adapter

- Implement `endpoint doctor` against `/slots`.
- Implement slot save/restore.
- Implement resume order:
  1. thread ledger
  2. capsule compatibility
  3. slot restore
  4. transcript diff append
  5. response checkpoint
- Add clear fallback behavior when restore fails.

Initial status:

- `scripts/capsule_cli.py` can create hard checkpoints with `checkpoint --hard --slot N`.
- `resume --thread X --slot N` restores the latest compatible hard capsule.
- `resume --append-diff` posts transcript messages after the checkpoint into the restored slot with `cache_prompt=true`.
- If hard restore fails, `resume --append-diff` marks that capsule `restore_failed`, replays the canonical transcript with `cache_prompt=false`, and saves a replacement hard checkpoint.
- The schema layer models `restore_failed` links and manifest `last_restore_failed_at` / `last_restore_error` lifecycle fields.
- `shutdown --thread X --slot N` saves a dirty thread before model unload.
- `scripts/test_capsule_cli_fake_llamacpp.py` validates the save/restore/append-diff request path and failed-restore fallback against a fake slot server.

## Open Questions

- Which `llama.cpp` server builds expose the most stable slot API fields?
- Which opencode hook should fill per-session capsule headers automatically instead of relying on launch-time environment variables?
- Should `.scap` include raw snapshots by default, or require an explicit `--include-snapshots` flag?
