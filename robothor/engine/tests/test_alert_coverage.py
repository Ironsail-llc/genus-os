"""Every ladder consumer must honor `alert`, not just one of them.

PR #190 made the alert rung real but wired notify_guardrail_alert() into the
exec_allowlist check alone. The other consumers still branch solely on
`== "enforce"` and fall through to the observe path, so promoting THOSE flags
to alert would still notify nobody — the same silent no-op, one layer down.

The original contract test only required >=1 consumer to act on alert, so it
passed while the gap remained. This pins per-consumer coverage.
"""

from __future__ import annotations

from pathlib import Path

import robothor.engine.feature_flags as ff

# (module, the ladder flag it consumes)
LADDER_CONSUMERS = [
    ("robothor/engine/guardrails.py", "exec_allowlist"),
    ("robothor/engine/runner.py", "completion_contract + sandbox_default"),
    ("robothor/memory/drift.py", "rip 7 (memory drift)"),
]

REPO_ROOT = Path(ff.__file__).resolve().parents[2]


def test_every_ladder_consumer_honors_alert():
    missing = []
    for rel, flag in LADDER_CONSUMERS:
        src = (REPO_ROOT / rel).read_text()
        if "notify_guardrail_alert" not in src:
            missing.append(f"{rel} ({flag})")
    assert not missing, (
        "these ladder consumers never call notify_guardrail_alert, so promoting "
        "their flag to 'alert' notifies nobody — the middle rung is a silent "
        f"no-op for them: {missing}"
    )


class TestAlertIsActuallyDelivered:
    """A row in a table nobody reads is not a notification.

    The agent-to-agent notification surface is effectively write-only:
    `send_notification`/`ack_notification` are registered as handlers but are
    NOT in tools/schemas.py (so no agent is even offered them), there is no
    read/list tool at all, and nothing in warmup or the heartbeat reads
    crm_agent_notifications. Only judge.py reads subjects, and the bridge API
    exposes them to the dashboard.

    So the DB row is an audit record, not delivery. An alert must also reach
    the operator on the channel they actually watch — the same Telegram path
    the failure pager and the soak nags already use.
    """

    def test_alert_reaches_telegram(self, monkeypatch):
        import robothor.crm.dal as dal
        import robothor.engine.feature_flags as ff

        monkeypatch.setattr(dal, "send_notification", lambda **kw: "id-1")
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("ROBOTHOR_TELEGRAM_CHAT_ID", "42")

        posted: list[str] = []
        monkeypatch.setattr(ff, "_post_telegram", lambda text: posted.append(text) or True)

        assert ff.notify_guardrail_alert(
            guardrail_name="exec_allowlist", agent_id="auto-agent", reason="chained shell"
        )

        assert posted, (
            "the alert was written to a table nobody reads and never reached the "
            "operator's channel — that is a record, not a notification"
        )
        assert "exec_allowlist" in posted[0]
