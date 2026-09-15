"""Three things a static analyser found that a behaviour test never would.

CodeQL failed the PR on this distribution, and each finding is real in a way the
suite could not see because the suite only ever asked whether the right answer
came back:

* **a secret in a log line.** The unconfigured-send warning named the credential
  by variable, and a variable called ``APP_PASSWORD_ENV`` is one refactor away
  from being the password rather than its name. The rule is not "mask it" — it is
  that a credential never reaches a log call at all, so the value is never one
  edit away from the journal;
* **log injection.** The ask id in a refusal line comes out of a card payload,
  which is a value a sender composes. A newline in it forges a second log line,
  and a forged log line is how an incident timeline gets written by the person
  being investigated;
* **super-linear matching.** ``<at>.*?</at>`` backtracks quadratically on
  repeated ``<at``, and the string it runs on is the message text of every
  inbound activity — an unauthenticated-by-content input on the request path.

The fourth CodeQL finding was in this suite's own fixtures (a host checked by
substring); it is fixed in ``test_outbound.py`` where it lives.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import pytest
from genus_teams import ask as ask_module
from genus_teams import channel as channel_module
from genus_teams import credentials as credentials_module
from genus_teams import router as router_module
from genus_teams.channel import TeamsChannel

#: Shaped like a real Entra client secret and visibly fake. If this string ever
#: appears in captured log output, something logged the credential itself.
FIXTURE_SECRET = "Q8n~aNotARealClientSecret.7xZq-3~kLm9"


class TestNoCredentialReachesALogLine:
    @pytest.mark.asyncio
    async def test_the_unconfigured_send_names_the_variables_not_the_values(
        self, monkeypatch, caplog
    ):
        """The path CodeQL flagged: a send on an instance with no credentials."""
        half = credentials_module.TeamsCredentials(app_id="00000000-0000", app_password=None)
        monkeypatch.setattr(channel_module, "teams_credentials", lambda **_kw: half)

        with caplog.at_level(logging.DEBUG):
            receipt = await TeamsChannel().send("29:somebody", "hello")

        assert receipt.status == "failed:teams_not_configured"
        # The operator still learns WHICH setting to fill in.
        assert "ROBOTHOR_TEAMS_APP_PASSWORD" in caplog.text

    @pytest.mark.asyncio
    async def test_a_configured_secret_never_appears_in_any_log_record(self, monkeypatch, caplog):
        """Every log line this send produces, with a real-shaped secret present."""
        configured = credentials_module.TeamsCredentials(
            app_id="00000000-0000-0000-0000-00000000aaaa",
            app_password=FIXTURE_SECRET,
            directory_tenant_id=None,
        )
        monkeypatch.setattr(channel_module, "teams_credentials", lambda **_kw: configured)
        monkeypatch.setattr(
            channel_module.conversations, "reference_for_target", lambda *_a, **_kw: None
        )

        with caplog.at_level(logging.DEBUG):
            await TeamsChannel().send("29:somebody", "hello")

        assert FIXTURE_SECRET not in caplog.text
        # Not a prefix of it either: a "masked" credential is still a credential
        # with its search space cut down.
        assert FIXTURE_SECRET[:8] not in caplog.text

    def test_the_send_path_passes_no_credential_value_to_a_logger(self):
        """AST: the warning takes the credential NAMES, never a value off the
        credentials object. A test that only reads captured output would pass on
        a line that logs a value which happened to be empty."""
        import ast
        from pathlib import Path

        source = Path(channel_module.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            if func.value.id != "logger":
                continue
            for argument in ast.walk(node):
                if isinstance(argument, ast.Attribute) and argument.attr in {
                    "app_password",
                    "app_id",
                    "token",
                }:
                    raise AssertionError(
                        f"a credential value is passed to logger.{func.attr} at line {node.lineno}"
                    )


class TestLogInjection:
    def test_an_ask_id_carrying_newlines_cannot_forge_a_log_line(self, caplog):
        """The ask id comes out of a card payload, which the sender composes."""
        forged = "abc\nWARNING genus_teams.ask: the operator approved everything"
        with caplog.at_level(logging.DEBUG):
            handled = ask_module.settle_from_activity(
                {"type": "message", "value": {ask_module.ASK_FIELD: forged}},
                conversation_id="19:a-room.thread.v2",
                native_id="somebody",
            )

        assert handled is True
        for record in caplog.records:
            assert "\n" not in record.getMessage(), "a sender wrote a second log line"

    def test_control_characters_are_escaped_rather_than_dropped(self, caplog):
        """Escaped, not stripped: an id that was tampered with should still be
        recognisable in the journal as the thing that arrived."""
        with caplog.at_level(logging.DEBUG):
            ask_module.settle_from_activity(
                {"type": "message", "value": {ask_module.ASK_FIELD: "a\rb"}},
                conversation_id="19:a-room.thread.v2",
                native_id="somebody",
            )
        assert any("\\r" in record.getMessage() for record in caplog.records)


class TestMentionStrippingIsLinear:
    #: Repeated unclosed opening tags: every ``<at>`` starts a match attempt and
    #: the lazy wildcard scans to the end of the string for each one. Measured on
    #: the pattern this replaces: 2,000 → 43 ms, 4,000 → 174 ms, 8,000 → 694 ms,
    #: quadrupling per doubling. A 240 KB message — inside the 256 KB body cap —
    #: is about forty seconds, on the request path, before the activity is even
    #: acknowledged.
    HOSTILE = "<at>" * 10_000

    def test_a_pathological_message_does_not_hang_the_endpoint(self):
        started = time.monotonic()
        router_module._text({"text": self.HOSTILE})
        assert time.monotonic() - started < 0.2, "mention stripping is super-linear"

    def test_a_longer_one_costs_proportionally_more_and_not_quadratically(self):
        def _elapsed(repeats: int) -> float:
            started = time.monotonic()
            router_module._text({"text": "<at>" * repeats})
            return time.monotonic() - started

        _elapsed(1_000)  # warm
        small = max(_elapsed(5_000), 1e-4)
        large = _elapsed(20_000)
        assert large / small < 12, f"4x the input cost {large / small:.1f}x the time"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<at>Genus</at> what is the plan", "what is the plan"),
            ("<AT>Genus</AT> shout", "shout"),
            ("before <at>Genus</at> after", "before  after"),
            ("<at>A</at> and <at>B</at> both", "and  both"),
            ("no mention here", "no mention here"),
            ("<at>unclosed and the rest", "<at>unclosed and the rest"),
            ("</at> stray close", "</at> stray close"),
            ("<at>outer <at>inner</at> tail", "tail"),
            ("", ""),
        ],
    )
    def test_it_still_strips_what_it_used_to(self, text: str, expected: str):
        assert router_module._text({"text": text}) == expected

    def test_it_matches_the_regex_it_replaced_on_ordinary_messages(self):
        """The shape that was there before, run over inputs a client actually
        sends, so the rewrite is a performance change and not a behaviour one."""
        previous = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)
        for text in (
            "<at>Genus</at> ship it",
            "plain text",
            "<at>Genus</at> and <at>Someone Else</at> please",
            "multi\nline <at>Genus</at> mention",
            "<at></at> empty",
        ):
            assert router_module._text({"text": text}) == previous.sub("", text).strip(), text


def test_no_module_in_this_distribution_uses_a_lazy_wildcard_between_tags() -> None:
    """The pattern, not this instance of it: ``.*?`` between two literals is the
    shape that backtracks, and the next one will be written by somebody who has
    forgotten this one."""
    import ast
    from pathlib import Path

    import genus_teams

    offenders: list[str] = []
    for path in sorted(Path(genus_teams.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Docstrings are exempt: this file's own explanation of why the lazy
        # wildcard was removed contains one, and a check that flagged the
        # explanation would be a check people delete.
        documentation = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        offenders.extend(
            f"{path.name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in documentation
            and (".*?" in node.value or ".+?" in node.value)
        )
    assert not offenders, f"lazy wildcard in a pattern at {offenders}"


def _unused(_: Any) -> None:  # pragma: no cover - keeps the import list honest
    return None
