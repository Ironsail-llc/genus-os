Agents can now read their organization's APM spans, single traces, monitors
and metrics in Datadog with `datadog_spans_search`, `datadog_trace_get`,
`datadog_monitors_list` and `datadog_metrics_query`. Like the log tools they
are read-only, opt-in through `tools_opt_in`, bounded by the tenant's Datadog
scope, and mask personal data; a trace from another organization reads as not
found. See [Tools](TOOLS.md#datadog-read-only) for following one failure from
a log line to its trace and to what is alerting.
