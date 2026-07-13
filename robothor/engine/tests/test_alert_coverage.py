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
