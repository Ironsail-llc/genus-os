"""A killed offline screening process must leave honest partial evidence."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def test_killed_screen_retains_finished_and_inflight_samples(tmp_path):
    output = tmp_path / "screen.json"
    journal = output.with_suffix(".json.jsonl")
    script = """
import asyncio
from bench.runtime import screen_candidates as screen
class Candidate:
    calls = 0
    def __init__(self, model): pass
    async def run(self, gateway, tenant):
        Candidate.calls += 1
        if Candidate.calls > 1:
            await asyncio.Event().wait()
        await gateway.dispatch(tenant, key="report", value="delivered")
        return {"verified": True, "model_calls": 1}
screen.PydanticCandidate = screen.DeepAgentsCandidate = Candidate
screen.pydantic_model = screen.deep_model = lambda: None
screen.importlib.metadata.version = lambda name: "fixture"
screen.main()
"""
    root = Path(__file__).resolve().parents[2]
    child = subprocess.Popen(
        [sys.executable, "-c", script, "--output", str(output)],
        cwd=root,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    events = []
    try:
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            if journal.exists():
                lines = journal.read_text().splitlines()
                try:
                    events = [json.loads(line) for line in lines]
                except json.JSONDecodeError:
                    continue
                if sum(event["event"] == "sample_started" for event in events) == 2:
                    break
            time.sleep(0.01)
        assert sum(event["event"] == "sample_started" for event in events) == 2
        child.kill()
        child.wait(timeout=5)
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        completed = [event for event in events if event["event"] == "sample_finished"]
        assert len(completed) == 1 and completed[0]["sample"]["status"] == "completed"
        assert completed[0]["sample"]["warmup"]
        assert events[-1]["event"] == "sample_started" and events[-1]["repetition"] == 1
        assert not output.exists()
        original = journal.read_bytes()
        retry = subprocess.run(
            [sys.executable, "-c", script, "--output", str(output)],
            cwd=root,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(root)},
            capture_output=True,
            timeout=5,
        )
        assert retry.returncode == 2
        assert b"Refusing to overwrite" in retry.stderr
        assert journal.read_bytes() == original and not output.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
