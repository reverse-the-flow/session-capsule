# opencode Native Hook Boundary

Verified against the public OpenCode docs on 2026-06-18:

- OpenCode custom providers support `options.headers`, which is enough for the current generated provider config path.
- OpenCode plugins support session events such as `session.created`, `session.updated`, `session.idle`, and `session.compacted`.
- OpenCode plugins also support `shell.env` for shell execution environments.
- The documented plugin hooks do not currently expose a provider-request or model-request header mutation hook.

Sources:

- https://opencode.ai/docs/providers/
- https://opencode.ai/docs/plugins

## Current Supported Path

Use the generated provider config:

```powershell
py -3 .\scripts\capsule_cli.py integration opencode-config --workspace . --session default --prefill user_default --out .\.capsules\integrations\opencode.generated.json
```

That writes concrete request headers into the provider config:

```text
X-Capsule-Workspace
X-Capsule-Thread
X-Capsule-Prefill
```

The gateway token remains an environment reference:

```text
{env:CAPSULE_GATEWAY_TOKEN}
```

This is the correct v0 integration because OpenCode sends provider `options.headers` with model requests, and the capsule gateway needs identity headers on those requests.

## Why Plugins Do Not Replace It Yet

The documented session plugin events are useful for observing lifecycle, but observation is not enough. A native replacement for generated provider configs must be able to attach capsule identity to the model request before it reaches the gateway.

The `shell.env` hook is also not enough. It affects shell execution environments; it does not prove that provider request headers can be computed per OpenCode session and injected into model calls.

## Required Future Hook

A native opencode replacement should have one of these shapes:

1. A provider-request hook that can mutate request headers before each model call.
2. A session-aware provider header template that can reference stable session id and workspace metadata.
3. A first-class provider metadata callback that returns headers for the active session.

The hook must provide or derive:

- stable opencode session id
- workspace path or workspace id
- selected model/provider id
- ability to set `X-Capsule-Thread`
- ability to set `X-Capsule-Workspace`
- optional ability to set `X-Capsule-Prefill`

## Decision

Do not add a passive watcher.

Do not replace generated provider configs with a plugin until OpenCode exposes a documented provider-request/header hook or session-aware provider header template.

Keep the gateway ready for native opencode by continuing to accept:

```text
X-Opencode-Thread
X-Opencode-Session
X-Opencode-Workspace
```

The generated config remains the supported path because it is explicit, request-path native, and testable.
