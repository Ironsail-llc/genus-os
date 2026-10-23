"""Tests for the devops team roster file loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.reports.devops_team import (
    TeamFileError,
    load_team,
    parse_team,
    runs_dir,
    team_file_path,
)


def _raw() -> dict:
    return {
        "roles": {
            "senior": {"title": "Senior Engineer", "focus": "Owns architecture"},
            "ai_reviewer": {"title": "AI Reviewer", "focus": "Reviews every PR"},
        },
        "members": [
            {
                "id": "alice",
                "name": "Alice Example",
                "role": "senior",
                "github": ["alice-gh", "alice-alt"],
                "jira": ["Alice Example"],
                "usage_name": "Alice",
            },
            {
                "id": "review-bot",
                "name": "Review Bot",
                "role": "ai_reviewer",
                "github": ["review-bot[bot]"],
            },
            {
                "id": "carol",
                "name": "Carol Former",
                "role": "senior",
                "github": ["carol-gh"],
                "active": False,
            },
        ],
        "bots_excluded": ["dependabot[bot]", "github-actions[bot]"],
    }


class TestParseTeam:
    def test_members_and_roles_load(self):
        team = parse_team(_raw())
        assert [m.id for m in team.members] == ["alice", "review-bot", "carol"]
        assert team.roles["senior"].title == "Senior Engineer"
        assert team.members[0].usage_name == "Alice"
        assert team.members[1].usage_name is None
        assert team.members[2].active is False

    def test_multiple_handles_map_to_one_member(self):
        team = parse_team(_raw())
        assert team.member_for("github", "alice-gh").id == "alice"
        assert team.member_for("github", "ALICE-ALT").id == "alice"
        assert team.member_for("jira", "Alice Example").id == "alice"

    def test_roster_bot_looking_member_is_a_member(self):
        team = parse_team(_raw())
        assert team.member_for("github", "review-bot[bot]").id == "review-bot"
        assert not team.is_bot("review-bot[bot]")

    def test_bots_excluded_and_unlisted_bot_suffix(self):
        team = parse_team(_raw())
        assert team.is_bot("dependabot[bot]")
        assert team.is_bot("some-other-app[bot]")
        assert not team.is_bot("alice-gh")

    def test_unknown_handle_is_none(self):
        assert parse_team(_raw()).member_for("github", "mallory") is None

    def test_duplicate_handle_across_members_is_an_error(self):
        raw = _raw()
        raw["members"][2]["github"] = ["alice-gh"]
        with pytest.raises(TeamFileError, match="alice-gh"):
            parse_team(raw)

    def test_unknown_role_is_an_error(self):
        raw = _raw()
        raw["members"][0]["role"] = "wizard"
        with pytest.raises(TeamFileError, match="wizard"):
            parse_team(raw)

    def test_duplicate_member_id_is_an_error(self):
        raw = _raw()
        raw["members"][1]["id"] = "alice"
        with pytest.raises(TeamFileError, match="alice"):
            parse_team(raw)

    def test_missing_members_is_an_error(self):
        with pytest.raises(TeamFileError):
            parse_team({"roles": {}})


class TestPaths:
    def test_team_file_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEVOPS_TEAM_FILE", str(tmp_path / "t.yaml"))
        assert team_file_path() == tmp_path / "t.yaml"

    def test_team_file_defaults_under_workspace(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEVOPS_TEAM_FILE", raising=False)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        assert team_file_path() == tmp_path / "local" / "devops" / "team.yaml"

    def test_runs_dir_defaults_under_workspace(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEVOPS_RUNS_DIR", raising=False)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        assert runs_dir() == tmp_path / "local" / "devops" / "runs"

    def test_load_team_from_yaml(self, tmp_path: Path):
        import yaml

        p = tmp_path / "team.yaml"
        p.write_text(yaml.safe_dump(_raw()))
        assert load_team(p).member_for("github", "carol-gh").id == "carol"

    def test_missing_file_is_an_error(self, tmp_path: Path):
        with pytest.raises(TeamFileError, match="not found"):
            load_team(tmp_path / "nope.yaml")
