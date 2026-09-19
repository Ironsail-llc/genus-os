Delegated agents now inherit their parent's managed fleet release. The engine
verifies and loads each child from that artifact, refusing drift or missing
members instead of falling back to live manifests. See `docs/AGENT_BUILDER.md`.
