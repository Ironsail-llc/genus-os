Datadog monitors can now report to Genus. Point a Datadog webhook at the
receiver's `/api/webhooks/datadog` channel, using the new `header_secret`
scheme with a shared secret in a custom header. Tag each monitor with
`org:<tenant id>`. Each alert, warn, no-data and recovery is recorded once as
a `datadog.alert_received` audit event in that tenant, together with who it
would notify and the read-only triage run it would start. For now
`ROBOTHOR_DATADOG_ALERTS_MODE` stays `dry_run`, so nothing is delivered and
no run starts. See [Observability](OBSERVABILITY.md#datadog-alerts-into-genus)
for the payload to paste into Datadog.
