"""Kill or cancel the real live harness with a nonexecuting fixture runner."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_live_harness_retains_sample_identity(tmp_path, cancel):
    output = tmp_path / "cohort.jsonl"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("model:\n  primary: openrouter/fixture/model\n")
    script = """
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from robothor.engine.tests.conftest import sample_agent_config
from robothor.engine.tests.test_runtime_live_native import test_configured_native_provider_cohort
class Runner:
    registry = Mock()
    config = SimpleNamespace(tenant_id="fixture")
    def __init__(self): self.entered = asyncio.Event()
    async def execute(self, *args, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()
async def main():
    engine = Runner()
    request = SimpleNamespace(getfixturevalue=lambda name: engine)
    task = asyncio.create_task(test_configured_native_provider_cohort(request, sample_agent_config.__wrapped__()))
    await engine.entered.wait()
    if CANCEL:
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        assert CANCEL
asyncio.run(main())
""".replace("CANCEL", str(cancel))
    root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(root),
        "ROBOTHOR_RUNTIME_LIVE_NATIVE": json.dumps(
            {"manifest": str(manifest), "output": str(output), "samples": 1, "diagnostics": True}
        ),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    journal = output.with_suffix(".events.jsonl")
    try:
        if cancel:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, (stdout + stderr).decode()
        else:
            deadline = time.monotonic() + 15
            while child.poll() is None and time.monotonic() < deadline:
                if journal.exists() and journal.stat().st_size:
                    break
                time.sleep(0.01)
            assert child.poll() is None and journal.exists() and journal.stat().st_size
            child.kill()
            child.wait(timeout=5)
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        assert events[0]["event"] == "sample_started"
        assert events[0]["model"] == "openrouter/fixture/model" and events[0]["repetition"] == 0
        if cancel:
            assert len(events) == 2 and events[-1]["event"] == "sample_finished"
            sample = events[-1]["sample"]
            assert sample == json.loads(output.read_text())
            assert sample["status"] == "interrupted" and sample["writes"] == 0
            assert sample["engine_cost_usd"] is None and sample["input_tokens"] is None
        else:
            assert len(events) == 1 and not output.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_deadline_failure_preserves_unknown_usage():
    from bench.runtime.native_journal import failed_sample
    from robothor.engine.runtime.deadlines import RuntimeDeadlineError

    row = failed_sample("fixture", 0, RuntimeDeadlineError("expired"), 60001)
    assert row["status"] == "timeout" and row["duration_ms"] == 60001
    assert row["engine_cost_usd"] is None and row["model_calls"] is None
