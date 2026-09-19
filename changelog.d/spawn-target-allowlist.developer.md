Agent manifests can declare `v2.spawn_allowed_agents` to limit delegation to
specific agent IDs. Targets are checked before manifest loading, and descendant
restrictions intersect with the ancestor's list instead of replacing it. Omitted
or empty lists preserve existing behavior. See `docs/AGENT_BUILDER.md`.
