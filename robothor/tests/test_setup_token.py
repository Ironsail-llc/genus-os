"""The one-shot credential that gets a browser to ``/setup`` and nothing else.

A fresh install has no account, so the wizard cannot be behind a session — and
anything reachable without a session on a box that holds every credential the
operator owns has to be narrow. The properties these tests hold down are the
ones that keep it narrow:

* the plaintext token exists in exactly one place, the operator's terminal —
  the file on disk holds a digest and could not re-issue the token if it were
  read;
* the comparison is constant-time, so the file's digest cannot be recovered a
  byte at a time by timing ``POST /api/setup/claim``;
* expiry, single use and "no file at all" are all refusals, and they are the
  same refusal from outside;
* the file is private to the operator (0600), because a token any local user
  can read is not a token.

``setup_complete`` is tested here too, and it is the one that matters most:
it asks the DATABASE whether an owner account exists. A completion signal
stored in a file would be removable by anyone who could delete the file, and
deleting a file is how you would re-open a first-run wizard on a running
appliance.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path  # noqa: TC003 - a pytest fixture annotation, resolved at runtime
from unittest.mock import patch

import pytest
import yaml

from robothor import setup_token

FIXTURE_TOKEN = "not-the-real-token"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway workspace. Never the operator's real one: the token file
    lands under ``<workspace>/.robothor/`` and this suite writes and deletes
    it."""
    return tmp_path / "workspace"


def _document(workspace: Path) -> dict:
    return yaml.safe_load(setup_token.token_path(workspace).read_text(encoding="utf-8"))


class TestCreate:
    def test_returns_the_token_and_stores_only_its_digest(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace)

        assert token
        document = _document(workspace)
        assert token not in yaml.safe_dump(document)
        assert document["token_sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_records_expiry_and_an_unconsumed_state(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace, ttl_seconds=1800)

        document = _document(workspace)
        assert document["consumed"] is False
        assert document["expires_at"].endswith("Z")

    def test_file_is_private_to_the_operator(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)

        mode = stat.S_IMODE(setup_token.token_path(workspace).stat().st_mode)
        assert mode == 0o600

    def test_a_second_token_replaces_the_first(self, workspace: Path) -> None:
        first = setup_token.create_setup_token(workspace)
        second = setup_token.create_setup_token(workspace)

        assert first != second
        assert setup_token.verify_setup_token(workspace, second) is True
        assert setup_token.verify_setup_token(workspace, first) is False

    def test_ttl_comes_from_the_declared_setting_when_none_is_passed(self, workspace: Path) -> None:
        """The setting must not be a control aimed at nothing."""
        from robothor.settings import get_settings

        with patch.object(setup_token, "configured_ttl_seconds", return_value=60) as configured:
            setup_token.create_setup_token(workspace)

        configured.assert_called_once()
        assert get_settings().auth.setup_token_ttl_seconds == 1800


class TestVerify:
    def test_accepts_the_token_it_issued(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace)

        assert setup_token.verify_setup_token(workspace, token) is True

    def test_rejects_a_different_token(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)

        assert setup_token.verify_setup_token(workspace, FIXTURE_TOKEN) is False

    def test_rejects_when_there_is_no_file(self, workspace: Path) -> None:
        assert setup_token.verify_setup_token(workspace, FIXTURE_TOKEN) is False

    def test_rejects_an_empty_token(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)

        assert setup_token.verify_setup_token(workspace, "") is False

    def test_rejects_an_expired_token(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace, ttl_seconds=1)
        document = _document(workspace)
        document["expires_at"] = "2000-01-01T00:00:00Z"
        setup_token.token_path(workspace).write_text(yaml.safe_dump(document), encoding="utf-8")

        assert setup_token.verify_setup_token(workspace, token) is False

    def test_rejects_a_consumed_token(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace)
        assert setup_token.consume_setup_token(workspace) is True

        assert setup_token.verify_setup_token(workspace, token) is False

    def test_rejects_a_malformed_file_without_raising(self, workspace: Path) -> None:
        path = setup_token.token_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this: [is not: valid", encoding="utf-8")

        assert setup_token.verify_setup_token(workspace, FIXTURE_TOKEN) is False

    def test_rejects_a_file_whose_document_is_not_a_mapping(self, workspace: Path) -> None:
        path = setup_token.token_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- a list\n", encoding="utf-8")

        assert setup_token.verify_setup_token(workspace, FIXTURE_TOKEN) is False

    def test_uses_a_constant_time_comparison(self, workspace: Path) -> None:
        """A byte-at-a-time comparison on a public route is a digest oracle."""
        token = setup_token.create_setup_token(workspace)

        with patch("hmac.compare_digest", return_value=True) as compare:
            assert setup_token.verify_setup_token(workspace, token) is True

        compare.assert_called_once()

    def test_compares_in_constant_time_even_with_no_file_at_all(self, workspace: Path) -> None:
        """The missing-file branch must cost what the present-file branch does,
        or the clock answers "has anyone run init here?"."""
        with patch("hmac.compare_digest", return_value=True) as compare:
            assert setup_token.verify_setup_token(workspace, FIXTURE_TOKEN) is False

        compare.assert_called_once()

    def test_a_true_comparison_cannot_override_expiry(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace, ttl_seconds=1)
        document = _document(workspace)
        document["expires_at"] = "2000-01-01T00:00:00Z"
        setup_token.token_path(workspace).write_text(yaml.safe_dump(document), encoding="utf-8")

        with patch("hmac.compare_digest", return_value=True):
            assert setup_token.verify_setup_token(workspace, token) is False


class TestConsume:
    def test_marks_the_document_consumed(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)

        assert setup_token.consume_setup_token(workspace) is True
        assert _document(workspace)["consumed"] is True

    def test_keeps_the_file_private(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)
        setup_token.consume_setup_token(workspace)

        mode = stat.S_IMODE(setup_token.token_path(workspace).stat().st_mode)
        assert mode == 0o600

    def test_is_false_when_there_is_nothing_to_consume(self, workspace: Path) -> None:
        assert setup_token.consume_setup_token(workspace) is False

    def test_never_writes_the_plaintext_token(self, workspace: Path) -> None:
        token = setup_token.create_setup_token(workspace)
        setup_token.consume_setup_token(workspace)

        assert token not in setup_token.token_path(workspace).read_text(encoding="utf-8")


class TestSetupComplete:
    def test_true_when_an_owner_account_exists(self, workspace: Path) -> None:
        with patch("robothor.auth.accounts.owner_account_exists", return_value=True):
            assert setup_token.setup_complete(workspace) is True

    def test_false_when_no_owner_account_exists(self, workspace: Path) -> None:
        with patch("robothor.auth.accounts.owner_account_exists", return_value=False):
            assert setup_token.setup_complete(workspace) is False

    def test_a_token_file_does_not_make_setup_incomplete(self, workspace: Path) -> None:
        """The signal is the database, not a file. A file-backed signal is a
        first-run wizard anyone who can delete a file can re-open."""
        setup_token.create_setup_token(workspace)

        with patch("robothor.auth.accounts.owner_account_exists", return_value=True):
            assert setup_token.setup_complete(workspace) is True

    def test_deleting_the_token_file_does_not_re_open_setup(self, workspace: Path) -> None:
        setup_token.create_setup_token(workspace)
        setup_token.token_path(workspace).unlink()

        with patch("robothor.auth.accounts.owner_account_exists", return_value=True):
            assert setup_token.setup_complete(workspace) is True

    def test_an_unreachable_database_reads_as_complete(self, workspace: Path) -> None:
        """Fail closed. "Nobody knows" must not publish a public write surface
        on a box that holds every credential the operator owns."""
        with patch(
            "robothor.auth.accounts.owner_account_exists",
            side_effect=RuntimeError("connection refused"),
        ):
            assert setup_token.setup_complete(workspace) is True


class TestSetupRecorded:
    """The SECOND question, and the reason there are two.

    ``setup_complete`` turns true halfway through the ceremony — the operator
    step creates the owner row — so it cannot also be what closes the wizard's
    later steps. Gating them on it shipped a wizard that killed itself at step
    2, with no state in which ``POST /api/setup/complete`` could succeed.
    """

    def test_false_before_anything_is_recorded(self, workspace: Path) -> None:
        assert setup_token.setup_recorded(workspace) is False

    def test_true_after_recording(self, workspace: Path) -> None:
        stamp = setup_token.record_setup_completed(workspace)

        assert stamp.endswith("Z")
        assert setup_token.setup_recorded(workspace) is True

    def test_the_marker_is_at_the_top_level_not_in_settings(self, workspace: Path) -> None:
        """``settings:`` is validated against the registry, and an undeclared
        key there is rejected under strict mode — a marker in it would stop a
        freshly completed instance from starting."""
        setup_token.record_setup_completed(workspace)

        document = yaml.safe_load(
            (workspace / ".robothor" / "config.yaml").read_text(encoding="utf-8")
        )
        assert document[setup_token.SETUP_COMPLETED_KEY].endswith("Z")
        assert setup_token.SETUP_COMPLETED_KEY not in (document.get("settings") or {})

    def test_recording_preserves_an_existing_config(self, workspace: Path) -> None:
        path = workspace / ".robothor" / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# hand-written\nsettings:\n  auth:\n    local_login: true\n", encoding="utf-8"
        )

        setup_token.record_setup_completed(workspace)

        text = path.read_text(encoding="utf-8")
        assert "# hand-written" in text
        assert "local_login: true" in text
        assert setup_token.setup_recorded(workspace) is True

    def test_an_unparseable_config_reads_as_recorded(self, workspace: Path) -> None:
        """Fail closed. "Nobody knows" must not re-open a public write surface."""
        path = workspace / ".robothor" / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this: [is not: valid", encoding="utf-8")

        assert setup_token.setup_recorded(workspace) is True

    def test_a_missing_file_is_not_an_unreadable_one(self, workspace: Path) -> None:
        """A config.yaml that simply does not exist yet is the fresh-install
        case, and must read as "still in setup" rather than as broken."""
        assert not (workspace / ".robothor" / "config.yaml").exists()

        assert setup_token.setup_recorded(workspace) is False

    def test_it_is_independent_of_the_database_answer(self, workspace: Path) -> None:
        """The two predicates must be able to disagree — that mid-ceremony
        state (an owner, nothing recorded) is the whole point."""
        with patch("robothor.auth.accounts.owner_account_exists", return_value=True):
            assert setup_token.setup_complete(workspace) is True
            assert setup_token.setup_recorded(workspace) is False


class TestSetupLink:
    def test_builds_the_url_the_operator_opens(self) -> None:
        link = setup_token.setup_link("127.0.0.1", 3004, FIXTURE_TOKEN)

        assert link == f"http://127.0.0.1:3004/setup?token={FIXTURE_TOKEN}"

    def test_percent_encodes_the_token(self) -> None:
        link = setup_token.setup_link("127.0.0.1", 3004, "a b/c")

        assert "a%20b%2Fc" in link

    def test_brackets_an_ipv6_host(self) -> None:
        link = setup_token.setup_link("::1", 3004, FIXTURE_TOKEN)

        assert link.startswith("http://[::1]:3004/setup?token=")

    @pytest.mark.parametrize(
        ("host", "loopback"),
        [
            ("127.0.0.1", True),
            ("localhost", True),
            ("::1", True),
            ("0.0.0.0", False),
            ("10.0.0.5", False),
            ("app.example.test", False),
        ],
    )
    def test_knows_which_hosts_a_browser_can_already_reach(self, host: str, loopback: bool) -> None:
        assert setup_token.is_loopback_host(host) is loopback

    def test_port_forward_hint_names_the_app_port(self) -> None:
        hint = setup_token.port_forward_hint("box.example.test", 3004)

        assert hint == "ssh -L 3004:127.0.0.1:3004 box.example.test"
