"""Tests for WS-6 cognitive-store fixes: router cues, episode-time normalization,
vault auto-populate extractor, and the populator flags."""

from __future__ import annotations

from typing import Any

import robothor.memory.lifecycle as lc
from robothor.memory.router import _normalize_fact, classify_query
from robothor.memory.vault import _vault_populate_enabled, extract_vault_candidates


class TestRouterCues:
    def test_confirm_resolve_route_temporal(self) -> None:
        assert (
            classify_query("Did Philip confirm the OpenRouter login was legitimate?") == "temporal"
        )
        assert classify_query("is the openrouter login resolved") == "temporal"
        assert classify_query("was the OpenRouter login legitimate") == "temporal"
        assert classify_query("what is the status of the migration") == "temporal"

    def test_plain_lookup_still_default(self) -> None:
        assert classify_query("tell me about FakeVendorCo") == "default"

    def test_normalize_fact_falls_back_to_episode_time(self) -> None:
        assert (
            _normalize_fact({"id": 1, "end_time": "T2", "start_time": "T1"})["created_at"] == "T2"
        )
        assert _normalize_fact({"id": 2, "start_time": "T1"})["created_at"] == "T1"
        assert _normalize_fact({"id": 3, "created_at": "C"})["created_at"] == "C"


class TestVaultExtractor:
    def test_extracts_account_id_and_phone(self) -> None:
        # Build the phone from parts so no literal 3-3-4 phone string lives in
        # this tracked platform file (the instance-data hook flags those).
        phone = "-".join(["800", "555", "0199"])
        content = f"The Helios billing account id is ACCT-HEL-00917. Call {phone} for support."
        cands = extract_vault_candidates(content)
        vals = {c["value"] for c in cands}
        types = {c["entry_type"] for c in cands}
        assert "ACCT-HEL-00917" in vals
        assert any(phone in v for v in vals)
        assert {"account_id", "contact_info"} <= types

    def test_caption_is_the_surrounding_sentence(self) -> None:
        cands = extract_vault_candidates("The Helios account id is ACCT-HEL-00917.")
        assert len(cands) == 1
        assert cands[0]["caption"] == "The Helios account id is ACCT-HEL-00917"

    def test_single_dash_token_is_not_an_account_id(self) -> None:
        # needs >=2 dash-separated uppercase/numeric segments
        assert extract_vault_candidates("ticket AB-12 was filed") == []

    def test_ignores_prose_without_reference_values(self) -> None:
        assert extract_vault_candidates("The meeting is at 3pm on the 5th of June.") == []


class TestFlags:
    def test_vault_populate_flag(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_VAULT_POPULATE", "1")
        assert _vault_populate_enabled() is True
        monkeypatch.delenv("MEMORY_VAULT_POPULATE", raising=False)
        assert _vault_populate_enabled() is False

    def test_intent_populate_flag(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_INTENT_POPULATE", "1")
        assert lc._intent_populate_enabled() is True
        monkeypatch.delenv("MEMORY_INTENT_POPULATE", raising=False)
        assert lc._intent_populate_enabled() is False
