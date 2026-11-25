Agents can now search and count their organization's logs in Datadog with
`datadog_logs_search` and `datadog_logs_aggregate`. Both are read-only and
opt-in: add them to a manifest's `tools_opt_in`. Every query is bounded by the
tenant's Datadog scope, email addresses and phone numbers in results are
masked, and a parent tenant's run can read a child with `tenant`. See
[Tools](TOOLS.md#datadog-read-only).
