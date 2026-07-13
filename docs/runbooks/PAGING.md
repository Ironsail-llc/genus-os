# Failure Paging (OnFailure → Telegram)

Any wired systemd unit that enters `failed` fires `robothor-alert@<unit>.service`,
which posts the unit name, host, and last journal lines to the operator's
Telegram via `scripts/send_failure_alert.sh`.

## Install on an instance

```bash
# 1. Alert template unit (adjust script path to your workspace):
sudo cp infra/systemd/robothor-alert@.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/robothor-alert@.service   # set ExecStart path

# 2. Wire the critical units:
sudo scripts/install_onfailure_alerts.sh \
    robothor-engine.service robothor-bridge.service robothor-orchestrator.service \
    robothor-nats.service robothor-delphi-engine.service
sudo systemctl daemon-reload
```

## Verify

```bash
sudo systemctl start robothor-alert@manual-test
# → a "🔴 manual-test FAILED" style message should arrive on Telegram
```

Scope deliberately small (~6 core units) to avoid alert fatigue; timers'
oneshot services can be added case-by-case once the baseline is quiet.
