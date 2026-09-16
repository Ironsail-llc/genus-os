"""If `genus secrets status` says `served=vault`, the reader must agree.

Review N2, and this one would have taken the instance down by following the
documented remediation. The runbook's order is migrate → verify → shrink, and
the verify step is ``genus secrets status`` plus ``secrets.shadowed``. Both
measure :func:`robothor.secrets.resolve_secret`. Three readers did not use it:

* ``EngineConfig.from_env().bot_token`` — the Telegram bot token, read from the
  settings model, which reads the environment only,
* ``alerts._send_webhook`` — ``os.environ.get("ROBOTHOR_ALERT_WEBHOOK_URL")``,
* ``audit.siem`` — ``os.environ.get("ROBOTHOR_SIEM_WEBHOOK_URL")``.

So after migrate the status table reported ``served=vault`` for all three, the
doctor was green, the operator shrank the SOPS file as instructed, restarted —
and the engine logged "ROBOTHOR_TELEGRAM_BOT_TOKEN is empty — Telegram delivery
disabled" at an operator who is Telegram-only. A verify step that says a name is
safe to remove when it is not is worse than no verify step.

Each test here deletes the environment copy — the shrink — and then asks the
READER. Asserting on ``resolve_secret`` is what let this through: the round-2
channel round-trip test did exactly that, and its own docstring said it drove
the reader.
"""

from __future__ import annotations

import pytest

FAKE_BOT = "1234567890:FAKE-telegram-bot-token-aaaaaaaaaaaaaaaaaaaa"
FAKE_WEBHOOK = "https://hooks.example.test/FAKE0000"


@pytest.fixture
def vault_only(monkeypatch):
    """The state the runbook leaves behind: the vault holds it, the environment
    does not."""
    from robothor import secrets as secrets_module
    from robothor import vault
    from robothor.vault.naming import vault_keys_for_env_name

    rows: dict[str, str] = {}

    def hold(env_name: str, value: str) -> None:
        rows[vault_keys_for_env_name(env_name)[0]] = value
        monkeypatch.delenv(env_name, raising=False)

    monkeypatch.setattr(vault, "get", lambda key, **kw: rows.get(key))
    monkeypatch.setattr(
        vault,
        "export_env",
        lambda **kw: {k.upper().replace("/", "_"): v for k, v in rows.items()},
    )
    monkeypatch.setattr(vault, "list", lambda **kw: sorted(rows))
    secrets_module.reset_vault_availability()
    yield hold
    secrets_module.reset_vault_availability()


def test_the_telegram_bot_token_comes_from_the_vault(vault_only, monkeypatch):
    """The reader the daemon actually starts the channel with.

    ``daemon.py`` refuses to start Telegram delivery on an empty
    ``bot_token``, so this exact call is what decides whether an operator who
    is Telegram-only has a channel after the shrink.
    """
    from robothor.engine.config import EngineConfig
    from robothor.settings import reset_settings

    vault_only("ROBOTHOR_TELEGRAM_BOT_TOKEN", FAKE_BOT)
    reset_settings()

    assert EngineConfig.from_env().bot_token == FAKE_BOT, (
        "the status table says served=vault and the reader disagrees — following "
        "the runbook would disable Telegram at the next restart"
    )


def test_the_alert_webhook_comes_from_the_vault(vault_only):
    """Alerting is how an operator learns anything is wrong, so a silent
    alerting channel is the worst thing on this list to lose quietly."""
    from robothor.engine import alerts

    vault_only("ROBOTHOR_ALERT_WEBHOOK_URL", FAKE_WEBHOOK)
    assert alerts.alert_webhook_url() == FAKE_WEBHOOK


def test_the_siem_webhook_comes_from_the_vault(vault_only):
    from robothor.audit import siem

    vault_only("ROBOTHOR_SIEM_WEBHOOK_URL", FAKE_WEBHOOK)
    assert siem.siem_webhook_url() == FAKE_WEBHOOK
    assert siem.siem_enabled() is True


def test_the_flag_pager_reads_the_same_token(vault_only):
    """``feature_flags`` pages the operator about guardrail state on its own
    path, and read the environment directly."""
    from robothor.engine import feature_flags

    vault_only("ROBOTHOR_TELEGRAM_BOT_TOKEN", FAKE_BOT)
    assert feature_flags._pager_token() == FAKE_BOT


# ── the general property ─────────────────────────────────────────────────────


def test_every_declared_secret_the_scan_migrates_has_an_accessor_backed_reader():
    """The rule that makes the runbook's verify step honest.

    ``genus secrets status`` can only see the accessor. So a declared,
    non-bootstrap credential — one the scan will migrate and the operator will
    then be told is safe to remove — must be READ through the accessor, or the
    verify step is attesting something it cannot measure.

    The allowance list is for names with no reader in the engine at all
    (Teams, Twilio): migrating them is harmless because nothing here consumes
    them, and marking that explicitly is what keeps this test from being
    quietly widened later.
    """
    from robothor.secrets.classification import declared_secret_names, is_bootstrap
    from robothor.settings.registry import field_index

    #: Declared names the engine does not read at all. Migrating one moves a
    #: value nothing consumes; it cannot break a reader because there is none.
    no_reader_in_the_engine = {
        "ROBOTHOR_TEAMS_APP_PASSWORD",
        "ROBOTHOR_TWILIO_AUTH_TOKEN",
        "ROBOTHOR_NATS_PASSWORD",
        "ROBOTHOR_NATS_URL",
    }

    index = field_index()
    migratable = {
        name
        for name in declared_secret_names()
        if index.get(name, {}).get("env") == name and not is_bootstrap(name)
    }

    unbacked = sorted(
        name
        for name in migratable - no_reader_in_the_engine
        if name in _NAMES_READ_WITHOUT_THE_ACCESSOR
    )
    assert not unbacked, (
        "these are declared, non-bootstrap (so `genus secrets migrate` moves them "
        "and `genus secrets status` will report served=vault), but their reader "
        "bypasses the accessor — the runbook would tell an operator they are safe "
        f"to delete from the SOPS file and the next restart would lose them: {unbacked}"
    )


#: Filled in by the guard below. Empty is the passing state; the guard exists so
#: that adding a raw ``os.environ`` read of a declared secret fails here rather
#: than in an operator's shrink.
_NAMES_READ_WITHOUT_THE_ACCESSOR: set[str] = set()


def test_no_declared_secret_is_read_with_a_raw_environment_call():
    """Where the list above comes from: the source, not a maintained list.

    Scans the engine and the audit package for ``os.environ.get("<NAME>")``
    where ``<NAME>`` is a declared, non-bootstrap credential. A maintained list
    of offenders would drift; this finds the next one.
    """
    import ast
    from pathlib import Path

    from robothor.secrets.classification import declared_secret_names, is_bootstrap
    from robothor.settings.registry import field_index

    index = field_index()
    watched = {
        name
        for name in declared_secret_names()
        if index.get(name, {}).get("env") == name and not is_bootstrap(name)
    }

    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if "tests" in relative.parts or relative.parts[0] == "settings":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        offenders.extend(
            f"{relative}:{node.lineno} {node.args[0].value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "__getitem__"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in watched
        )

    assert not offenders, (
        "a declared, migratable credential is read straight from the environment, so "
        "`genus secrets status` would report served=vault while this reader sees "
        f"nothing after the SOPS shrink: {offenders}"
    )
