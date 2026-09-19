Sub-agent tool overrides can now narrow a child's declared allowlist but cannot
expand it. An invalid override returns an error before the child runs; an empty
list preserves its manifest settings. Callers that previously added tools through
an override must update the child manifest deliberately. See `docs/AGENT_BUILDER.md`.
