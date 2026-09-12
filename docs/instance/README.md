# Instance runbooks

Everything in this directory describes **the first Genus OS instance** — one
box, with its own hardware, its own backup volume and its own measured
numbers. They are kept in the repository because the mechanisms they document
(the thermal guard, the backup volume guard) ship in `scripts/` and
`infra/systemd/`, and because a second operator standing up the same guard
wants to see what a real deployment measured.

They are **not** platform documentation. Do not copy a threshold or an
incident date out of them into a page an operator reads as a promise about
their own hardware — re-measure on the box in front of you.

| Runbook | What it describes |
|---------|-------------------|
| [`THERMAL.md`](THERMAL.md) | The measured thermal envelope of the reference appliance, and the thresholds derived from it |
| [`BACKUP_VOLUME_GUARD.md`](BACKUP_VOLUME_GUARD.md) | The encrypted USB backup volume of the first instance, its guard, and its incident log |

Platform runbooks — the ones any instance can follow — stay in
[`docs/runbooks/`](../runbooks/).
