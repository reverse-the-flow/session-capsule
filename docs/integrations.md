# Session Capsule Integrations

## Purpose

Integrations should make existing clients speak to the capsule gateway. They should not scrape browser state, watch logs, or own runtime slots.

```text
client
  -> OpenAI-compatible capsule gateway
    -> model endpoint
```

The client integration has only three jobs:

- point the client at the gateway base URL
- pass stable thread or workspace metadata when the client can expose it
- keep streaming disabled until the gateway supports streaming

## Gateway Target

Start the gateway first:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode soft
```

For hard local capsules:

```powershell
py -3 .\scripts\capsule_gateway.py --state-dir .\.capsules --endpoint local-llamacpp --port 8765 --checkpoint-mode hard --slot 0
```

Clients should use:

```text
http://127.0.0.1:8765/v1
```

If the client runs inside Docker while the gateway runs on the Windows host, use:

```text
http://host.docker.internal:8765/v1
```

If the gateway is launched with `--auth-token-file` or `--auth-token-env`, use that token as the OpenAI-compatible API key. Control-plane tools may send the same value as `X-Capsule-Gateway-Key`.

## Headers

Preferred explicit headers:

- `X-Capsule-Thread`
- `X-Capsule-Workspace`
- `X-Capsule-Prefill`

The gateway also recognizes common client-native identity headers:

- `X-OpenWebUI-Chat-Id`
- `X-OpenWebUI-User-Id`
- `X-Opencode-Thread`
- `X-Opencode-Session`
- `X-Opencode-Workspace`
- `X-Session-Id`
- `X-Conversation-Id`
- `X-Workspace-Id`

If no usable thread header exists, the gateway creates a conservative generated thread id from the model and first request message. That works for smoke testing but gives weaker continuity than an explicit id.

The minimum useful metadata is:

| Client | Required for continuity | Optional workspace metadata |
| --- | --- | --- |
| Generic OpenAI-compatible client | `X-Capsule-Thread` | `X-Capsule-Workspace` |
| Open WebUI | `X-OpenWebUI-Chat-Id` | `X-OpenWebUI-User-Id` |
| opencode | `X-Opencode-Thread` or `X-Opencode-Session` | `X-Opencode-Workspace` |

The running gateway advertises the same contract from `/api/capsules/status` under `identity`.

## Open WebUI

Open WebUI can be pointed at the gateway as an OpenAI-compatible API base URL.

Use the environment example in:

```text
examples/integrations/open-webui.env.example
```

For Docker on Windows, the important values are:

```text
ENABLE_OPENAI_API=True
OPENAI_API_BASE_URL=http://host.docker.internal:8765/v1
OPENAI_API_KEY=sk-capsule-local
ENABLE_FORWARD_USER_INFO_HEADERS=True
```

`ENABLE_FORWARD_USER_INFO_HEADERS=True` lets Open WebUI forward chat and user headers. The gateway maps `X-OpenWebUI-Chat-Id` to `X-Capsule-Thread` behavior and `X-OpenWebUI-User-Id` to workspace metadata.

If gateway auth is enabled, set `OPENAI_API_KEY` to the gateway token.

## opencode

opencode can use an OpenAI-compatible custom provider.

Generate a provider config with concrete workspace/thread/prefill headers:

```powershell
py -3 .\scripts\capsule_cli.py integration opencode-config --workspace . --session default --prefill user_default --out .\.capsules\integrations\opencode.generated.json
```

The generated config keeps the gateway token as an environment reference:

```text
{env:CAPSULE_GATEWAY_TOKEN}
```

Set `CAPSULE_GATEWAY_TOKEN` only when gateway auth is enabled. Do not write the token into the generated config.

The static template remains in:

```text
examples/integrations/opencode.capsule-provider.jsonc
```

The core provider shape is:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "session-capsules": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Session Capsules",
      "options": {
        "baseURL": "http://127.0.0.1:8765/v1",
        "apiKey": "{env:CAPSULE_GATEWAY_TOKEN}",
        "headers": {
          "X-Capsule-Workspace": "{env:CAPSULE_WORKSPACE}",
          "X-Capsule-Prefill": "{env:CAPSULE_PREFILL}",
          "X-Capsule-Thread": "{env:CAPSULE_THREAD}"
        }
      },
      "models": {
        "fake-model": {
          "name": "Capsule Gateway Model"
        }
      }
    }
  },
  "model": "session-capsules/fake-model"
}
```

The generated config is the CLI-first path when no native opencode provider-request hook is available. It avoids relying on hand-set `CAPSULE_THREAD`, `CAPSULE_WORKSPACE`, and `CAPSULE_PREFILL` values by writing those headers directly into the provider config for the selected workspace and session.

Current native-hook decision:

```text
docs/opencode-native-hook.md
```

As of the current OpenCode docs reviewed on 2026-06-18, custom provider `options.headers` are the supported request-path mechanism. Plugins expose session events and shell environment hooks, but no documented provider-request/header mutation hook. A native replacement should wait for a hook that can compute headers per active session before each model call.

On Windows, a minimal launcher example is:

```powershell
.\examples\integrations\start-opencode-capsule.ps1 -Prefill user_default
```

It sets a workspace-derived `CAPSULE_THREAD` when one is not supplied and then starts opencode with the capsule provider model.

## Status And Checkpoint Controls

The gateway exposes small local control endpoints for UI surfaces, shell helpers, or manual checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/capsules/status
Invoke-RestMethod http://127.0.0.1:8765/api/capsules/threads
```

Create an explicit checkpoint:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/checkpoint `
  -Body '{"thread_id":"THREAD","mode":"soft"}'
```

Hard local checkpoint:

```powershell
Invoke-RestMethod `
  -Method Post `
  -ContentType "application/json" `
  -Uri http://127.0.0.1:8765/api/capsules/checkpoint `
  -Body '{"thread_id":"THREAD","mode":"hard","slot":0}'
```

## Bundle Transport Controls

The gateway also exposes `.scap` upload/download controls for local UIs and Model Plane:

```text
GET    /api/capsules/status
POST   /api/capsules/export
GET    /api/capsules/bundles
POST   /api/capsules/bundles
GET    /api/capsules/bundles/{bundle_id}
POST   /api/capsules/import
DELETE /api/capsules/bundles/{bundle_id}
```

Model Plane should read `/api/capsules/status` first and use its `transport` object to discover the concrete endpoint paths, content type, max upload size, auth policy, signing policy, and available upload/download capabilities for the running gateway.

Use `POST /api/capsules/bundles` for store-only upload when the user wants to transfer a `.scap` without immediately importing it into local thread state.

Transport details are in:

```text
docs/transport.md
```

## Integration Rule

The gateway is the missing layer. App integrations should stay thin:

- no model weight transport
- no direct KV access
- no browser scraping
- no attempt to infer hidden prompt state after the request has already left the client

When a client cannot pass a thread id, use the gateway anyway and accept generated thread IDs until that client has a proper hook.
