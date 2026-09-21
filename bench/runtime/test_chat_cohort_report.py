"""Incomplete and failed chat conversations remain in the reported population."""

import json

from bench.runtime.chat_cohort_report import report
from bench.runtime.native_journal import NativeJournal


def test_failed_and_unfinished_conversations_remain_visible(tmp_path):
    journal = NativeJournal(tmp_path / "cohort.jsonl")
    journal.record("sample_started", "model", 0)
    (tmp_path / "sample-000.json").write_text(
        json.dumps(
            {
                "transcript": [{"elapsed_ms": 60005, "run": {"status": "timeout"}}],
                "goal_status": "waiting",
                "task_statuses": ["DONE", "TODO"],
            }
        )
    )
    journal.record("sample_finished", "model", 0, exit_code=1, artifact="sample-000.json")
    journal.record("sample_started", "model", 1)
    result = report(tmp_path)
    assert result["counts"] == {"passed": 0, "failed": 1, "unresolved": 1, "not_started": 28}
    assert result["elapsed_ms"]["status"]["p95"] == 60005
    assert result["elapsed_ms"]["status"]["missing"] == 29
    assert result["elapsed_ms"]["pause"]["missing"] == 30
    assert not result["complete_population"] and not result["manual_acceptance"]
