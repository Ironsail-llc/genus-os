Native sales stages now pass the delivery enum expected by AgentRunner, fixing a
failure before the first model call. Spawn contexts consistently describe the
executing run's depth, so ordinary and benchmark children record depth one rather
than double-counting it. A full-runner integration test exercises real role checks,
tool dispatch, shared request accounting, and persisted parent/child records.
