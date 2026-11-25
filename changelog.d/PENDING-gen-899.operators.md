Datadog watch findings and Datadog monitor alerts are now delivered, each in
the tenant it belongs to and by that tenant's own engine: watch findings go to
the main agent's alert digest once per episode and never page, and a
`Triggered` monitor alert pages that tenant's operator and starts one
read-only triage run of its main agent. An alert for a tenant with no engine
reading Datadog alerts is recorded there as `delivery_failed` and the root
gets one quiet notice. See [Observability](OBSERVABILITY.md#datadog-alerts-into-genus)
for the levels, the episode and triage rules, and how to roll back.
