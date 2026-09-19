Each benchmark judge attempt now has a 30-second wall-clock deadline in addition
to the provider timeout. Stalled grading returns a judge error and retains the
uncertain request charge; retries still share the original allowance. See
`docs/runbooks/BENCHMARK_HARNESS_FAIRNESS.md`.
