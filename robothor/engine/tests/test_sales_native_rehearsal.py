"""Offline native workflow rehearsal: real dispatch, workers and durable state.

LLM business outputs and external provider effects are fixtures. No customer
account or live model is contacted. Every tick reconstructs the workflow engine.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from robothor.sales.tests.test_service import sales as sales


@pytest.mark.asyncio
async def test_native_workflows_drive_the_complete_component_rehearsal(
    sales, engine_config, monkeypatch
):
    from robothor.engine.models import WorkflowDef, WorkflowStepDef, WorkflowStepType
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.engine.workflow import WorkflowEngine
    from robothor.sales import queue
    from robothor.sales.tests import test_pipeline_rehearsal as rehearsal

    engine_config = replace(engine_config, tenant_id=sales.tenant)
    stages = {
        "ScoutWorker": "scout",
        "ResearchWorker": "research",
        "ContactWorker": "contacts",
        "VerificationWorker": "verify",
        "PromotionWorker": "promotion",
        "DraftWorker": "draft",
        "DeliveryWorker": "delivery",
        "ConversationWorker": "conversation",
        "StopWorker": "stop",
        "ActivationWorker": "activation",
    }
    bindings = {stage: "fixture-sales-" + stage for stage in [*stages.values(), "qualify"]}
    sales.configure({"workflow_bindings": bindings}, "operator:fixture")
    runs = []

    class Pump:
        def __init__(self, worker, class_name):
            self.worker, self.class_name = worker, class_name

        async def execute(self, stage):
            monkeypatch.setattr(queue, self.class_name, lambda service: self.worker)
            engine = WorkflowEngine(engine_config, runner=SimpleNamespace(registry=ToolRegistry()))
            workflow = WorkflowDef(
                id=bindings[stage],
                name="Fixture sales workflow",
                timeout_seconds=60,
                steps=[
                    WorkflowStepDef(
                        id="advance",
                        type=WorkflowStepType.TOOL,
                        tool_name="sales_process_queue",
                        tool_args={"stage": stage},
                        retry_count=0,
                    )
                ],
            )
            engine._workflows[workflow.id] = workflow
            run = await engine.execute(workflow.id, trigger_type="cron")
            runs.append(run)
            assert str(run.status) == "completed", (run.error_message, run.context)
            value = run.context["steps"]["advance"]["tool_output"]
            return (json.loads(value) if isinstance(value, str) else value)["worked"]

        async def tick(self):
            return await self.execute(stages[self.class_name])

        async def qualify_tick(self):
            return await self.execute("qualify")

    for name in stages:
        constructor = getattr(rehearsal, name)
        monkeypatch.setattr(
            rehearsal,
            name,
            lambda *args, _ctor=constructor, _name=name, **kw: Pump(_ctor(*args, **kw), _name),
        )
    await rehearsal.test_discovery_to_reviewed_outreach_reply_optout_and_fulfillment(sales)
    assert len(runs) == 19
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM workflow_runs WHERE tenant_id=%s AND status='completed'",
            (sales.tenant,),
        )
        assert cur.fetchone()["n"] == len(runs)
