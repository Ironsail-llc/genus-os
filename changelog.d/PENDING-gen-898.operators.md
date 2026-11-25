The Datadog watch now also notices a tenant's Datadog monitors that start
firing (Alert, Warn or No Data) or recover, and a service that was logging
steadily and has gone quiet, recording again when it logs. Like the error-spike
findings, these are `datadog.watch_finding` audit events marked `dry_run`, and
nothing is sent. A monitor also wired to Genus's Datadog webhook appears in
both records. See [Observability](OBSERVABILITY.md#genus-watches-datadog).
