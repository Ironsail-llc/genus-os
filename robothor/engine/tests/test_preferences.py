"""Tests for preference tracking with drift detection."""

from __future__ import annotations

import json
from unittest.mock import patch

from robothor.memory import preferences
from robothor.memory.preferences import (
    _format_summary,
    _match_existing,
    get_stale_preferences,
)


class TestMatchExisting:
    def test_no_match_returns_none(self):
        prefs = [{"preference": "Prefers dark mode"}]
        assert _match_existing(prefs, "Enjoys peanut butter") is None

    def test_exact_match(self):
        prefs = [{"preference": "Prefers dark mode"}]
        assert _match_existing(prefs, "prefers dark mode") is not None

    def test_substring_match(self):
        prefs = [{"preference": "Prefers dark mode"}]
        # New phrasing contains the existing one
        assert _match_existing(prefs, "Prefers dark mode over light") is not None


class TestFormatSummary:
    def test_empty_preferences(self):
        assert _format_summary([]).startswith("No tracked")

    def test_orders_by_confidence(self):
        prefs = [
            {"preference": "Low conf pref", "confidence": 0.3},
            {"preference": "High conf pref", "confidence": 0.9},
        ]
        summary = _format_summary(prefs)
        assert summary.index("High conf pref") < summary.index("Low conf pref")

    def test_stale_marker(self):
        prefs = [{"preference": "Stale thing", "confidence": 0.5, "stale": True}]
        assert "[STALE]" in _format_summary(prefs)


class TestStaleReadback:
    """get_stale_preferences should return only stale entries from the block."""

    def test_returns_only_stale(self):
        fake_block = {
            "content": json.dumps(
                {
                    "preferences": [
                        {"preference": "A", "stale": False},
                        {"preference": "B", "stale": True},
                        {"preference": "C"},  # missing → treat as not stale
                    ]
                }
            )
        }
        with patch("robothor.memory.preferences.read_block", return_value=fake_block):
            result = get_stale_preferences(tenant_id="test")
        assert [p["preference"] for p in result] == ["B"]


class TestPersistenceRoundtrip:
    """_load_preferences + _save_preferences handles empty/corrupt block gracefully."""

    def test_load_empty_block(self):
        from robothor.memory.preferences import _load_preferences

        with patch("robothor.memory.preferences.read_block", return_value={"content": ""}):
            assert _load_preferences("test") == []

    def test_load_corrupt_block(self):
        from robothor.memory.preferences import _load_preferences

        with patch(
            "robothor.memory.preferences.read_block",
            return_value={"content": "not json"},
        ):
            assert _load_preferences("test") == []


class TestSaveLoadRoundTrip:
    """What the writer produces, the reader must accept.

    Every other test in this file hands `_load_preferences` a fake block
    containing clean JSON. None of them has ever fed it what `_save_preferences`
    actually writes — and that writer prepends a plain-text summary before the
    JSON, so `json.loads()` on the whole string throws every single time and the
    handler logs "starting fresh" and returns [].

    Measured on production 2026-08-22: 115 occurrences in 24 hours, on both
    tenants. The one preference being tracked was
    "Prefers 'ox alpha' in the model list and Telegram model picker" — the
    operator's own request, learned and then discarded on every read.

    A double that only ever supplies well-formed input cannot catch a writer
    that emits ill-formed output.
    """

    @staticmethod
    def _roundtrip(prefs):
        """Save, then load, through a real in-memory block store."""
        store: dict[str, str] = {}

        def _write(name, content, tenant_id=None):
            store[name] = content

        def _read(name, tenant_id=None):
            return {"content": store.get(name, "")}

        with (
            patch("robothor.memory.preferences.write_block", _write),
            patch("robothor.memory.preferences.read_block", _read),
        ):
            preferences._save_preferences(prefs, tenant_id="t")
            return preferences._load_preferences(tenant_id="t"), store

    def test_what_is_saved_can_be_loaded(self):
        prefs = [{"preference": "Prefers ox alpha in the model picker", "confidence": 0.55}]
        loaded, _ = self._roundtrip(prefs)
        assert loaded, "the writer's own output did not survive a read"
        assert loaded[0]["preference"] == prefs[0]["preference"]

    def test_the_block_is_valid_json(self):
        """It is parsed with json.loads, so it must be JSON — all of it."""
        _, store = self._roundtrip([{"preference": "p", "confidence": 0.9}])
        json.loads(store["preferences"])

    def test_confidence_survives(self):
        loaded, _ = self._roundtrip([{"preference": "p", "confidence": 0.42}])
        assert loaded[0]["confidence"] == 0.42

    def test_several_preferences_survive(self):
        prefs = [{"preference": f"p{i}", "confidence": 0.5} for i in range(4)]
        loaded, _ = self._roundtrip(prefs)
        assert len(loaded) == 4

    def test_a_preference_containing_braces_survives(self):
        """Operator text is arbitrary; a brace must not break the parse."""
        prefs = [{"preference": 'Use {"model": "ox-alpha"} in config', "confidence": 0.5}]
        loaded, _ = self._roundtrip(prefs)
        assert loaded and loaded[0]["preference"] == prefs[0]["preference"]

    def test_the_summary_is_still_available(self):
        """The human-readable rollup was the point of the prepend — keep it."""
        _, store = self._roundtrip([{"preference": "readable", "confidence": 0.7}])
        assert "readable" in store["preferences"]
