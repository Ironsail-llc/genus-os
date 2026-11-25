Genus now watches each tenant's Datadog telemetry on its own: every 5 minutes
it notices a service whose error logs jump well above that service's own
recent level, and the root tenant's engine notices when the Datadog key is
rejected or Datadog cannot be reached. It starts in dry run: each finding is a
`datadog.watch_finding` audit event holding the brief it would send and to
whom, and nothing is delivered until you set `ROBOTHOR_DATADOG_WATCH_MODE=deliver`.
See [Observability](OBSERVABILITY.md#genus-watches-datadog).
