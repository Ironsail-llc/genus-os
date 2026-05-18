# Codex Subscription Provider

Genus OS can route `codex/*` model IDs through the local Codex CLI session so
usage is backed by ChatGPT/Codex subscription auth instead of an OpenRouter or
OpenAI API key.

## Auth

Log in as the same Unix user that runs the Genus engine:

```bash
robothor codex login
robothor codex doctor
```

Use the ChatGPT browser sign-in flow. Do not use an OpenAI API key for this
provider path; API keys are usage-billed by OpenAI Platform. Genus strips
`OPENAI_API_KEY` and related OpenAI API environment variables from `codex/*`
subprocess calls to avoid accidentally switching billing modes.

If the engine runs as a service user, either run the login command as that user
or set `ROBOTHOR_CODEX_HOME` to a service-owned Codex auth directory.

## Model IDs

Use `codex/` model IDs in agent manifests:

```yaml
model:
  primary: codex/gpt-5.5
  fallbacks:
    - openrouter/xiaomi/mimo-v2.5-pro
```

Current registry entries:

- `codex/gpt-5.5`
- `codex/gpt-5.4`
- `codex/gpt-5.3-codex`

## Behavior

The provider supports text-only and tool-using Genus turns through `codex exec`.
For tool-using turns, Genus presents its OpenAI-compatible tool schemas to Codex
as host-executed tools and constrains Codex to return structured JSON. Genus then
converts that response into normal OpenAI-style `tool_calls`, so the existing
engine remains responsible for tool execution, telemetry, RBAC, and audit.

This is intentionally a provider replacement, not an engine replacement.

Current tradeoff: each Codex provider call shells out to `codex exec`. That keeps
the implementation simple and uses the official ChatGPT-authenticated Codex CLI,
but it is slower than a long-lived app-server transport. A future optimization can
swap the transport while preserving the same provider contract.

## Verification

```bash
robothor codex status
robothor codex test "Reply with exactly: genus codex ok" --model codex/gpt-5.5
```
