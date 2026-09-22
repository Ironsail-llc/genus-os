"""Reject rollback code that finalizes a compound request after one goal report."""

import json
import os
import subprocess
import sys


def probe(code):
    script = """
import json
from pathlib import Path
from types import SimpleNamespace
from robothor.engine import goal_report_delivery as delivery
from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession
from robothor.engine.tools.dispatch import ToolContext
from robothor.goals.report_channel import publish_report
assert Path(delivery.__file__).resolve().is_relative_to(Path.cwd())
results = {}
for compound in (False, True):
    session = AgentSession('main', TriggerType.WEBCHAT, tenant_id='synthetic')
    session.run.task_text = "What's finished, and what's still left?"
    if compound:
        session.run.task_text += ' Also calculate 17 times 19.'
    req = SimpleNamespace(session=session, readonly_mode=False)
    ctx = ToolContext(agent_id='main', tenant_id='synthetic', run_id=session.run.id)
    with delivery.report_scope(req, ['report_pursuit_goal']) as state:
        publish_report(ctx, 'One task remains open; the goal is incomplete.')
    delivery.record_report_turn(state, session, [])
    results['compound' if compound else 'standalone'] = delivery.finish_goal_report(session)
print(json.dumps({'compatible': results == {'standalone': True, 'compound': False},
                  'finalized': results}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=code,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(code), "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Rollback report-boundary probe failed: " + result.stderr)
    return json.loads(result.stdout)
