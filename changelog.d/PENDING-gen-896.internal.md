The Datadog client now reads spans, single traces, monitors and metrics as
well as logs, each bounded by the matching part of the tenant's scope. Nothing
calls these reads yet; agent tools and dashboard views build on them. See
[Observability](OBSERVABILITY.md#reading-datadog-from-genus).
