Datadog watch findings and Datadog monitor alerts are now delivered, each in
the tenant it belongs to and by that tenant's own engine, once per episode:
only a triggered alert for a monitor with priority P1 pages, and everything
else goes to the main agent's alert digest. Every triggered alert also starts
one read-only triage run of that tenant's main agent, and a monitor wired to
the webhook is not sent a second time by the watch. An alert for a tenant with
no engine reading Datadog alerts is recorded there as `delivery_failed` and
the root gets one quiet notice. See [Observability](OBSERVABILITY.md#datadog-alerts-into-genus)
for the levels, the episode and triage rules, and how to roll back.
