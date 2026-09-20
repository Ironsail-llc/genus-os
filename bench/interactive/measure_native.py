"""Offline microbenchmark of attendee merge/verification, never real Google.

Run from the repository root: python bench/interactive/measure_native.py
This excludes authentication, DB, runner setup, model and network latency.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from robothor.engine.calendar_attendees import add_attendees  # noqa: E402


class FixtureCalendar:
    def __init__(self):
        self.event = {
            "id": "fixture",
            "etag": '"v1"',
            "summary": "Fixture only",
            "attendees": [{"email": "existing@example.test", "responseStatus": "accepted"}],
            "start": {"dateTime": "2026-09-22T16:00:00-04:00"},
            "end": {"dateTime": "2026-09-22T16:30:00-04:00"},
        }
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, calendar_id, event_id, **kwargs):
        self.calls.append(method)
        if method == "PATCH":
            assert kwargs["etag"] == self.event["etag"]
            self.event.update(deepcopy(kwargs["body"]))
            self.event["etag"] = '"v2"'
        return deepcopy(self.event)


def main():
    samples = []
    for _ in range(200):
        api = FixtureCalendar()
        before = deepcopy(api.event)
        with patch("robothor.engine.calendar_attendees.CalendarTransport", return_value=api):
            start = time.perf_counter()
            result = add_attendees(
                "fixture", "fixture", ["new@example.test"], screen=lambda *a: None
            )
            samples.append((time.perf_counter() - start) * 1000)
        assert result["verification"] == "verified"
        assert api.calls == ["GET", "PATCH", "GET"]
        assert api.event["attendees"][0] == before["attendees"][0]
        assert api.event["start"] == before["start"] and api.event["end"] == before["end"]
        assert len(api.event["attendees"]) == 2
    print(
        json.dumps(
            {
                "benchmark": "native_operation_microbenchmark",
                "samples": len(samples),
                "scope": "in-memory merge/verification only; excludes DB, runner, auth, model and network",
                "p50_ms": statistics.median(samples),
                "p95_ms": sorted(samples)[189],
                "state_checks_passed": True,
                "model_calls": 0,
                "fixture_api_calls": 3,
                "writes": 1,
                "post_completion_tool_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
