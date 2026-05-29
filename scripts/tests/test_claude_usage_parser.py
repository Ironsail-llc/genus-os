"""Tests for the Claude Usage Report parser used by the devops pipeline.

The 'Claude Usage Interceptor' automation emails a plaintext report weekly
to robothor@example.com. We parse it into structured per-user records so
the DevOps report can include Claude adoption metrics.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "claude_usage_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_usage_report", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_usage_report"] = mod
    spec.loader.exec_module(mod)
    return mod


SAMPLE_BODY = """Claude Usage Report

Week of May 18, 2026 – May 24, 2026

Generated Tue, 19 May 2026 19:06:46 GMT
Total users
12
10 missing this week
Near-limit events
1
5-hr util ≥ 80% across all users
Extra usage cost
0.00
Top: adan@example.com
Highest peak util
11%
adan@example.com
User breakdown Attention first · missing last
User Avg util (7-day) Peak util Week trend Near-limit events Days w/
data Latest
activity Extra usage cost Errors
adan@example.com
11%
11%
0% 1× 1 / 7 May 19, 07:03 PM UTC 0.00 0
nadjib@example.com
4%
4%
0% 0 1 / 7 May 19, 05:43 PM UTC 0.00 0
danylo@example.com — — — — No data — — —
rochelle@example.com — — — — No data — — —
jhonray@example.com — — — — No data — — —
lorenz@example.com — — — — No data — — —
Shahbaz@example.com — — — — No data — — —
muhammad@example.com — — — — No data — — —
rizzi@example.com — — — — No data — — —
hr@example.com — — — — No data — — —
illia@example.com — — — — No data — — —
jawad@example.com — — — — No data — — —
Automated report · Claude Usage Interceptor · IronSail
"""


class TestParser:
    def test_extracts_period(self):
        mod = _load()
        r = mod.parse_report(SAMPLE_BODY)
        assert r["period"] == "Week of May 18, 2026 – May 24, 2026"
        assert r["generated_at"].startswith("Tue, 19 May 2026")

    def test_extracts_topline_metrics(self):
        mod = _load()
        r = mod.parse_report(SAMPLE_BODY)
        assert r["total_users"] == 12
        assert r["missing_users"] == 10
        assert r["near_limit_events"] == 1
        assert r["extra_usage_cost_usd"] == 0.0
        assert r["top_user"] == "adan@example.com"
        assert r["highest_peak_util_pct"] == 11

    def test_extracts_per_user_active(self):
        mod = _load()
        r = mod.parse_report(SAMPLE_BODY)
        users_by_email = {u["email"]: u for u in r["users"]}
        adan = users_by_email["adan@example.com"]
        assert adan["has_data"] is True
        assert adan["avg_util_pct"] == 11
        assert adan["peak_util_pct"] == 11
        assert adan["near_limit_events"] == 1
        assert adan["days_with_data"] == 1
        assert adan["errors"] == 0
        nadjib = users_by_email["nadjib@example.com"]
        assert nadjib["has_data"] is True
        assert nadjib["peak_util_pct"] == 4

    def test_extracts_per_user_missing(self):
        mod = _load()
        r = mod.parse_report(SAMPLE_BODY)
        users_by_email = {u["email"]: u for u in r["users"]}
        danylo = users_by_email["danylo@example.com"]
        assert danylo["has_data"] is False
        assert danylo["avg_util_pct"] is None
        assert danylo["peak_util_pct"] is None

    def test_user_count_matches_total(self):
        mod = _load()
        r = mod.parse_report(SAMPLE_BODY)
        assert len(r["users"]) == 12
        active = [u for u in r["users"] if u["has_data"]]
        missing = [u for u in r["users"] if not u["has_data"]]
        assert len(active) == 2
        assert len(missing) == 10


class TestBodyFilter:
    """Guards the bug where the fetcher grabbed a Drive share-notification
    email instead of the real report (both share the subject line)."""

    def test_real_report_body_accepted(self):
        mod = _load()
        assert mod._looks_like_report_body(SAMPLE_BODY) is True

    def test_drive_share_notification_rejected(self):
        mod = _load()
        notification = (
            "I've shared an item with you:\n\n"
            "Claude Usage Report\n"
            "https://drive.google.com/drive/folders/abc123\n\n"
            "It's not an attachment -- it's stored online."
        )
        assert mod._looks_like_report_body(notification) is False

    def test_empty_body_rejected(self):
        mod = _load()
        assert mod._looks_like_report_body("") is False


class TestExtractTextBody:
    def test_walks_nested_multipart(self):
        import base64

        mod = _load()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(b"Week of X User breakdown").decode()
                    },
                },
                {"mimeType": "text/html", "body": {"data": ""}},
            ],
        }
        out = mod._extract_text_body(payload)
        assert "Week of X" in out
        assert "User breakdown" in out
