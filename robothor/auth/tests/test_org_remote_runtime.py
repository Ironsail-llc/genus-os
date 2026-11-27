"""The remote-tools mode and the deployment's attestations, as callers read them."""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from robothor.auth.runtime import org_remote_attestations, org_remote_tools_mode
from robothor.flags import store

FLAG = "GENUS_ORG_REMOTE_TOOLS"


@pytest.fixture
def no_db_row():
    with mock.patch.object(store, "resolve", side_effect=lambda name: None):
        yield


def test_unset_is_full(monkeypatch, no_db_row):
    monkeypatch.delenv(FLAG, raising=False)
    assert org_remote_tools_mode() == "full"


@pytest.mark.parametrize("rung", ["off", "read", "dry_run", "full"])
def test_each_rung_from_the_environment(monkeypatch, rung):
    monkeypatch.setenv(FLAG, rung)
    with mock.patch.object(store, "resolve", side_effect=lambda name: None):
        assert org_remote_tools_mode() == rung


def test_an_unknown_value_is_off_and_logged(monkeypatch, no_db_row, caplog):
    monkeypatch.setenv(FLAG, "mostly")
    with caplog.at_level(logging.WARNING, logger="robothor.auth.runtime"):
        assert org_remote_tools_mode() == "off"
    assert any(FLAG in r.getMessage() and "mostly" in r.getMessage() for r in caplog.records)


def test_the_flag_store_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(FLAG, "full")
    with mock.patch.object(store, "resolve", side_effect=lambda name: "read") as resolve:
        assert org_remote_tools_mode() == "read"
    resolve.assert_called_with(FLAG)


def test_an_unknown_store_value_is_off(monkeypatch):
    monkeypatch.setenv(FLAG, "full")
    with mock.patch.object(store, "resolve", side_effect=lambda name: "bogus"):
        assert org_remote_tools_mode() == "off"


def test_the_flag_is_governed():
    from robothor.flags.store import GOVERNED_FLAGS, default_value_for, valid_values_for

    assert FLAG in GOVERNED_FLAGS
    assert valid_values_for(FLAG) == ("off", "read", "dry_run", "full")
    assert default_value_for(FLAG) == "full"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("mostly", "off"), ("", "full"), ("read", "read"), ("  dry_run ", "dry_run")],
)
def test_the_store_shows_what_the_accessor_runs(raw, expected):
    assert store.normalise(FLAG, raw) == expected


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
