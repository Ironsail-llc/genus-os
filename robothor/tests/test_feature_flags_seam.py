"""A DB operator row must change what the engine's readers return — no restart."""

from __future__ import annotations

from robothor.engine import feature_flags
from robothor.flags import store


def test_rip7_mode_reads_the_store(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setattr(
        store, "resolve", lambda name: "enforce" if name == "ROBOTHOR_RIP_7_MODE" else None
    )
    store.invalidate()
    assert feature_flags.rip_7_enforcement_mode() == "enforce"


def test_env_still_works_when_store_returns_none(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RIP_7_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RIP_7_MODE", "alert")
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.rip_7_enforcement_mode() == "alert"


def test_do_not_contact_mode_reads_the_store(monkeypatch):
    """The compliance flag is governed like every other guardrail mode.

    It shipped reading os.environ directly, which meant the one control that
    can silence itself was also the one control an operator could not see in
    /api/controls, could not flip from the dashboard, and whose flip left no
    audit row. Governed flags resolve DB-first precisely so a change is a
    recorded decision rather than an untraceable edit on a box.
    """
    monkeypatch.setenv("ROBOTHOR_DNC_MODE", "enforce")
    monkeypatch.setattr(
        store, "resolve", lambda name: "observe" if name == "ROBOTHOR_DNC_MODE" else None
    )
    store.invalidate()
    assert feature_flags.do_not_contact_mode() == "observe"


def test_do_not_contact_mode_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_DNC_MODE", "observe")
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.do_not_contact_mode() == "observe"


def test_do_not_contact_defaults_to_enforce(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_DNC_MODE", raising=False)
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.do_not_contact_mode() == "enforce"


def test_an_unrecognised_do_not_contact_value_enforces(monkeypatch, caplog):
    """A typo must not switch off a compliance control, and must say so."""
    monkeypatch.setattr(store, "resolve", lambda name: None)
    for value in ("off", "alert", "disabled", "OBSERVE_ALL"):
        monkeypatch.setenv("ROBOTHOR_DNC_MODE", value)
        with caplog.at_level("WARNING"):
            caplog.clear()
            assert feature_flags.do_not_contact_mode() == "enforce", value
        assert any("ROBOTHOR_DNC_MODE" in r.message for r in caplog.records), value


def test_the_panic_switch_does_not_disable_the_opt_out(monkeypatch):
    """ROBOTHOR_DISABLE_ALL_RIPS forces every NEW behaviour dark. This is not
    one of those: it is a legal opt-out, and a panic switch that mails the
    people who asked not to be mailed is not a safe state to panic into. Every
    other flag in this module honours the switch; this one deliberately does
    not, and that difference has to be pinned or someone will 'fix' it."""
    monkeypatch.setenv("ROBOTHOR_DISABLE_ALL_RIPS", "1")
    monkeypatch.delenv("ROBOTHOR_DNC_MODE", raising=False)
    monkeypatch.setattr(store, "resolve", lambda name: None)
    assert feature_flags.do_not_contact_mode() == "enforce"


def test_the_mode_is_read_at_call_time_not_cached(monkeypatch):
    """An operator flipping the flag must not have to wait out a cache. The
    store's TTL cache holds the DB answer; when that is None the env is read
    live on every call, which is what makes `systemctl set-environment` plus a
    restart-free flip work at all."""
    monkeypatch.setattr(store, "resolve", lambda name: None)
    monkeypatch.setenv("ROBOTHOR_DNC_MODE", "observe")
    assert feature_flags.do_not_contact_mode() == "observe"
    monkeypatch.setenv("ROBOTHOR_DNC_MODE", "enforce")
    assert feature_flags.do_not_contact_mode() == "enforce"


def test_the_dnc_flag_only_offers_the_rungs_the_engine_honours(monkeypatch):
    """valid_values_for drives both the API's 422 and the dashboard's picker.
    Offering `off`/`alert` for a flag the engine maps to `enforce` would let an
    operator set a rung, see it stored, and get different behaviour."""
    assert store.valid_values_for("ROBOTHOR_DNC_MODE") == ("observe", "enforce")
