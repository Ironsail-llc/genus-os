"""The deployment's remote-tools attestations, as callers read them."""

from __future__ import annotations

import logging

from robothor.auth.runtime import org_remote_attestations


def test_attestations_default_empty(monkeypatch):
    monkeypatch.delenv("GENUS_ORG_REMOTE_ATTESTATIONS", raising=False)
    assert org_remote_attestations() == frozenset()


def test_attestations_parse_a_comma_list(monkeypatch):
    monkeypatch.setenv("GENUS_ORG_REMOTE_ATTESTATIONS", " baa, hipaa_2 ,,baa")
    assert org_remote_attestations() == frozenset({"baa", "hipaa_2"})


def test_attestations_drop_malformed_tokens_and_log(monkeypatch, caplog):
    monkeypatch.setenv("GENUS_ORG_REMOTE_ATTESTATIONS", "baa,Not-A-Token,9x")
    with caplog.at_level(logging.WARNING, logger="robothor.auth.runtime"):
        assert org_remote_attestations() == frozenset({"baa"})
    assert any("Not-A-Token" in r.getMessage() for r in caplog.records)
