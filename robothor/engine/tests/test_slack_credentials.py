"""One reader for this instance's Slack credentials — or four surfaces disagree.

The defect this pins shipped in the first cut of the channel: ``genus channel
add slack`` writes both tokens to the **vault** whenever the instance has a
master key, which is the normal full install. But the daemon's inbound gate and
``SlackBot.start`` each read ``os.environ`` directly, and the doctor read
``ctx.settings`` — neither of which consults the vault, and nothing preloads
channel credentials into the process environment (``key_pool`` filters its
vault→env export down to *provider* key names).

So one box answered the question "is Slack configured?" three different ways at
once: ``genus channel list`` said yes, ``genus doctor`` said "not configured",
and the Socket Mode bot silently never started — the env gate logs nothing at
all when it declines. Meanwhile ``genus channel verify slack`` printed four
green rows, including one whose docstring claims it proves "whether the inbound
half will start".

Every surface now asks :func:`slack_credentials`, so there is one answer. These
tests are the proof: with the tokens ONLY in a vault, all three say configured;
with them nowhere, all three say not configured.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

import robothor
from robothor.engine.channels.slack_credentials import (
    APP_TOKEN_ENV,
    APP_TOKEN_VAULT_KEY,
    BOT_TOKEN_ENV,
    BOT_TOKEN_VAULT_KEY,
    slack_credentials,
)

FAKE_BOT_TOKEN = "xoxb-test-not-a-real-token"
FAKE_APP_TOKEN = "xapp-test-not-a-real-token"


@pytest.fixture(autouse=True)
def _no_ambient_slack(monkeypatch, tmp_path):
    """Neither this box's environment nor its workspace reaches a test."""
    from robothor.settings import reset_settings

    monkeypatch.delenv(BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(APP_TOKEN_ENV, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def vault_only(monkeypatch):
    """Both tokens in a vault and nowhere else — the default ``add`` destination."""
    rows = {BOT_TOKEN_VAULT_KEY: FAKE_BOT_TOKEN, APP_TOKEN_VAULT_KEY: FAKE_APP_TOKEN}

    def _read(name: str, vault_key: str | None, tenant_id: str, *, live: bool = False):
        return rows.get(vault_key or ""), True

    monkeypatch.setattr("robothor.secrets._vault_read", _read)
    return rows


@pytest.fixture
def empty_vault(monkeypatch):
    """A vault that answers and holds nothing. Not an unavailable one."""

    def _read(name: str, vault_key: str | None, tenant_id: str, *, live: bool = False):
        return None, True

    monkeypatch.setattr("robothor.secrets._vault_read", _read)


class TestTheReaderItself:
    def test_the_environment_wins_over_the_vault(self, vault_only, monkeypatch):
        """An operator who exported a token meant it, and a rotation that
        reaches the environment must not be shadowed by a stale vault row."""
        monkeypatch.setenv(BOT_TOKEN_ENV, "xoxb-test-exported-token")
        found = slack_credentials()

        assert found.bot_token == "xoxb-test-exported-token"
        assert found.bot_source == "env"
        assert found.app_source == "vault"

    def test_the_vault_answers_when_the_environment_does_not(self, vault_only):
        found = slack_credentials()
        assert (found.bot_token, found.app_token) == (FAKE_BOT_TOKEN, FAKE_APP_TOKEN)
        assert found.can_send is True
        assert found.can_listen is True

    def test_nothing_anywhere_is_not_configured(self, empty_vault):
        found = slack_credentials()
        assert (found.bot_token, found.app_token) == (None, None)
        assert found.can_send is False
        assert found.can_listen is False

    def test_a_bot_token_alone_can_send_but_not_listen(self, empty_vault, monkeypatch):
        """The two halves are independent: posting a briefing needs no socket."""
        monkeypatch.setenv(BOT_TOKEN_ENV, FAKE_BOT_TOKEN)
        found = slack_credentials()
        assert found.can_send is True
        assert found.can_listen is False


class TestEverySurfaceAgrees:
    """The three judges of "is Slack configured?" on one box."""

    @staticmethod
    async def _daemon_started_slack() -> bool:
        from robothor.engine import daemon

        tasks: list[Any] = []
        bot = await daemon._start_channels(SimpleNamespace(), SimpleNamespace(), tasks)
        for task in tasks:
            task.cancel()
        return bot is not None and any(task.get_name() == "slack" for task in tasks)

    @staticmethod
    async def _doctor_says_configured() -> bool:
        from robothor.doctor.checks import channels as channel_checks
        from robothor.doctor.context import DoctorContext

        check = next(item for item in channel_checks.CHECKS if item.id == "slack.token")
        answer = await check.run(DoctorContext(timeout_s=2.0, offline=True))
        rows = answer if isinstance(answer, list) else [answer]
        return any(row.sub_id == "token" and row.status == "pass" for row in rows)

    @staticmethod
    async def _channel_says_configured() -> bool:
        from robothor.engine.channels.slack import SlackChannel

        return bool((await SlackChannel().health())["configured"])

    @pytest.mark.asyncio
    async def test_tokens_only_in_the_vault_configure_every_surface(self, vault_only):
        """The install `genus channel add slack` produces by default.

        Before this, the daemon gate read an empty environment and the inbound
        bot never started, while `verify` reported Socket Mode green.
        """
        assert await self._daemon_started_slack() is True, (
            "the daemon's Slack gate is blind to the vault, so the inbound bot "
            "never starts on the CLI's own default destination"
        )
        assert await self._doctor_says_configured() is True, (
            "genus doctor reports an instance unconfigured that genus channel "
            "list reports configured"
        )
        assert await self._channel_says_configured() is True

    @pytest.mark.asyncio
    async def test_tokens_nowhere_configure_nothing(self, empty_vault):
        assert await self._daemon_started_slack() is False
        assert await self._doctor_says_configured() is False
        assert await self._channel_says_configured() is False


#: The module allowed to spell a Slack token name. Everything else in
#: ``robothor/`` has to go through :func:`slack_credentials`.
CREDENTIAL_OWNER = "robothor/engine/channels/slack_credentials.py"


def _literal_strings(node: ast.AST) -> list[str]:
    """Every string a node evaluates to, for the spellings a grep would miss.

    A plain ``ast.Constant`` is the easy case. ``"ROBOTHOR_SLACK_" + "BOT_TOKEN"``
    and an f-string of constant parts both produce the same name at runtime and
    neither shows up as one literal, so a guard that only looked at
    ``ast.Constant`` could be walked straight past — by accident as easily as on
    purpose, since line-length reformatting is how a long name gets split in the
    first place.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_strings(node.left), _literal_strings(node.right)
        return [a + b for a in left for b in right]
    if isinstance(node, ast.JoinedStr):  # an f-string of constant parts only
        parts: list[str] = []
        for value in node.values:
            found = _literal_strings(value)
            if not found:
                return []
            parts.append(found[0])
        return ["".join(parts)]
    return []


def token_name_reads(source: str) -> list[int]:
    """Line numbers where ``source`` reads a Slack token name out of the environment.

    Two access shapes, because the first cut of this guard only knew one:

    * a CALL — ``os.environ.get(...)``, ``os.getenv(...)``, and the mutating
      ``setdefault``/``pop`` accessors;
    * a SUBSCRIPT — ``os.environ["ROBOTHOR_SLACK_BOT_TOKEN"]``, which is the
      terser spelling and was invisible to the call-only version.

    Deliberately shape-based rather than name-based: it does not check that the
    object is ``os.environ``, because anything that reads one of these two names
    out of any mapping is doing the thing this guard exists to stop.
    """
    tree = ast.parse(source)
    names = {BOT_TOKEN_ENV, APP_TOKEN_ENV}
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            accessor = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if accessor in {"get", "getenv", "setdefault", "pop"} and any(
                value in names for arg in node.args for value in _literal_strings(arg)
            ):
                found.append(node.lineno)
        elif isinstance(node, ast.Subscript) and any(
            value in names for value in _literal_strings(node.slice)
        ):
            found.append(node.lineno)
    return sorted(found)


class TestNoSurfaceReadsTheEnvironmentDirectly:
    """A second reader is how the four surfaces drifted apart in the first place.

    The first cut of this guard named five modules and looked for one access
    shape. Both halves of that are the thing it is guarding against: a sixth
    module is exactly what a future change adds, and ``os.environ["…"]`` is a
    spelling it could not see. It now walks the whole package.
    """

    @staticmethod
    def _package_modules() -> list[pathlib.Path]:
        root = pathlib.Path(robothor.__file__).parent
        return [
            path
            for path in sorted(root.rglob("*.py"))
            # Test files configure the test, not the product — and several
            # legitimately construct these names to set up an environment.
            if "tests" not in path.parts and not path.name.startswith("test_")
        ]

    def test_the_scan_actually_reaches_the_modules_it_guards(self):
        """A scan that matches nothing passes forever. Name the files."""
        scanned = {str(path) for path in self._package_modules()}
        for required in (
            "engine/slack.py",
            "engine/daemon.py",
            "engine/channels/slack.py",
            "doctor/checks/channels.py",
            "cli/channel.py",
            CREDENTIAL_OWNER.split("robothor/", 1)[1],
        ):
            assert any(path.endswith(required) for path in scanned), (
                f"the guard no longer walks {required}"
            )
        assert len(scanned) > 200, f"only {len(scanned)} modules scanned; the walk is broken"

    def test_the_detector_finds_every_spelling(self):
        """Proved against synthetic source, so the guard is known to bite before
        it is pointed at a package that (correctly) contains nothing."""
        for spelling in (
            f'os.environ.get("{BOT_TOKEN_ENV}")',
            f'os.getenv("{APP_TOKEN_ENV}")',
            f'os.environ["{BOT_TOKEN_ENV}"]',
            f'os.environ.pop("{APP_TOKEN_ENV}", None)',
            f'os.environ.setdefault("{BOT_TOKEN_ENV}", "")',
            'os.environ.get("ROBOTHOR_SLACK_" + "BOT_TOKEN")',
            'os.environ["ROBOTHOR_" f"SLACK_APP_TOKEN"]',
            f'settings.get("{BOT_TOKEN_ENV}")',
        ):
            assert token_name_reads(spelling), f"the guard cannot see {spelling!r}"

    def test_the_detector_does_not_cry_wolf(self):
        for innocent in (
            'os.environ.get("ROBOTHOR_SLACK_ALLOWED_USERS")',
            'os.environ.get("ROBOTHOR_CHANNELS")',
            f'"{BOT_TOKEN_ENV}"',
            f'logger.warning("%s is unset", "{BOT_TOKEN_ENV}")',
            f'raise ValueError("{BOT_TOKEN_ENV} is not set")',
        ):
            assert not token_name_reads(innocent), f"false positive on {innocent!r}"

    def test_the_owner_is_the_only_module_that_spells_these_names(self):
        offenders: list[str] = []
        owner_reads = 0
        for path in self._package_modules():
            reads = token_name_reads(path.read_text(encoding="utf-8"))
            if not reads:
                continue
            if str(path).endswith(CREDENTIAL_OWNER):
                owner_reads += len(reads)
                continue
            offenders.extend(f"{path}:{line}" for line in reads)

        assert not offenders, (
            "these read a Slack token name out of the environment themselves "
            f"instead of calling slack_credentials(): {offenders}"
        )
