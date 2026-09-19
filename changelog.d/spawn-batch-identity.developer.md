Parallel `spawn_agents` now forwards the authenticated tool context to every
child, matching single-agent delegation instead of dropping tenant and caller identity.
