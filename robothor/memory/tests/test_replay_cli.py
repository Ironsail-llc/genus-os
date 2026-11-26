"""``genus memory-replay``: the flags, the tenant gate and where the report goes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.cli import memory_replay
from robothor.memory import replay

TENANT = "test-tenant"


def _parse(*argv: str) -> Any:
    from robothor.cli import _build_parser

    return _build_parser().parse_args(["memory-replay", *argv])


class TestParser:
    def test_defaults(self, tmp_path: Any) -> None:
        args = _parse("--tenant", TENANT, "--from-runs")
        assert args.command == "memory-replay"
        assert args.tenant == TENANT
        assert args.from_runs is True
        assert args.limit == 50
        assert args.sample is False
        assert args.compare is None
        assert args.json_output is False
        assert args.redact is False

    def test_every_flag(self, tmp_path: Any) -> None:
        args = _parse(
            "--tenant", TENANT,
            "--questions", str(tmp_path / "q.txt"),
            "--as-person", "11111111-1111-4111-8111-111111111111",
            "--as-user", "u1",
            "--since", "7d",
            "--limit", "40",
            "--sample", "--seed", "3",
            "--compare", "new-graph,new-segments",
            "--json", "--out", str(tmp_path / "r.json"),
            "--redact",
        )  # fmt: skip
        assert args.questions == str(tmp_path / "q.txt")
        assert args.as_person == "11111111-1111-4111-8111-111111111111"
        assert args.as_user == "u1"
        assert (args.since, args.limit, args.sample, args.seed) == ("7d", 40, True, 3)
        assert args.compare == "new-graph,new-segments"
        assert args.json_output and args.redact

    def test_tenant_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse("--from-runs")

    def test_one_source_at_a_time(self) -> None:
        with pytest.raises(SystemExit):
            _parse("--tenant", TENANT, "--from-runs", "--from-chat")
        with pytest.raises(SystemExit):
            _parse("--tenant", TENANT)


@pytest.fixture
def fake_replay(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The library half stubbed: the tenant exists, the search is canned."""
    seen: dict[str, Any] = {}

    def require_tenant(tenant_id: str) -> None:
        seen["tenant_checked"] = tenant_id

    async def search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.setdefault("calls", []).append({"query": query, **kwargs})
        return [
            {
                "id": 1,
                "kind": "fact",
                "fact_text": "Alice keeps the spare key under the blue flowerpot",
                "leg_ranks": {"vector": 0},
                "relevance": 0.7,
            }
        ]

    monkeypatch.setattr(replay, "require_tenant", require_tenant)
    monkeypatch.setattr(replay, "default_search", lambda: search)
    return seen


def _questions_file(tmp_path: Any) -> str:
    path = tmp_path / "questions.txt"
    path.write_text("where is the spare key?\nwhen is the dentist?\n")
    return str(path)


class TestCommand:
    def test_text_report_with_the_notice_first(
        self, tmp_path: Any, fake_replay: dict[str, Any], capsys: Any
    ) -> None:
        args = _parse("--tenant", TENANT, "--questions", _questions_file(tmp_path))
        assert memory_replay.cmd_memory_replay(args) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == replay.NOTICE
        assert "where is the spare key?" in out
        assert fake_replay["tenant_checked"] == TENANT

    def test_json_report(self, tmp_path: Any, fake_replay: dict[str, Any], capsys: Any) -> None:
        args = _parse(
            "--tenant", TENANT, "--questions", _questions_file(tmp_path),
            "--as-person", "11111111-1111-4111-8111-111111111111", "--json",
        )  # fmt: skip
        assert memory_replay.cmd_memory_replay(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["notice"] == replay.NOTICE
        assert report["compare"] == ["today", "new"]
        assert len(report["questions"]) == 2
        assert {c["reader_person_id"] for c in fake_replay["calls"]} == {
            "11111111-1111-4111-8111-111111111111"
        }

    def test_out_writes_the_report(
        self, tmp_path: Any, fake_replay: dict[str, Any], capsys: Any
    ) -> None:
        target = tmp_path / "report.json"
        args = _parse(
            "--tenant", TENANT, "--questions", _questions_file(tmp_path),
            "--json", "--out", str(target), "--redact",
        )  # fmt: skip
        assert memory_replay.cmd_memory_replay(args) == 0
        report = json.loads(target.read_text())
        assert report["redacted"] is True
        assert "flowerpot" not in target.read_text()
        out = capsys.readouterr().out
        assert out.splitlines()[0] == replay.NOTICE
        assert str(target) in out
        assert "flowerpot" not in out

    def test_an_unknown_tenant_is_refused(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        def require_tenant(tenant_id: str) -> None:
            raise LookupError(f"unknown tenant {tenant_id!r}")

        monkeypatch.setattr(replay, "require_tenant", require_tenant)
        args = _parse("--tenant", "no-such-tenant", "--questions", _questions_file(tmp_path))
        assert memory_replay.cmd_memory_replay(args) == 2
        assert "unknown tenant" in capsys.readouterr().err

    def test_as_person_needs_a_question_file(
        self, fake_replay: dict[str, Any], capsys: Any
    ) -> None:
        args = _parse("--tenant", TENANT, "--from-runs", "--as-person", "p1")
        assert memory_replay.cmd_memory_replay(args) == 2
        assert "--questions" in capsys.readouterr().err

    def test_an_unknown_preset_is_refused(
        self, tmp_path: Any, fake_replay: dict[str, Any], capsys: Any
    ) -> None:
        args = _parse(
            "--tenant", TENANT, "--questions", _questions_file(tmp_path), "--compare", "today,v9"
        )
        assert memory_replay.cmd_memory_replay(args) == 2
        assert "v9" in capsys.readouterr().err

    def test_the_whole_command_runs_read_only(
        self, tmp_path: Any, fake_replay: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.db import connection

        seen: list[bool] = []

        async def search(query: str, **_kw: Any) -> list[dict[str, Any]]:
            seen.append(connection.read_only_active())
            return []

        monkeypatch.setattr(replay, "default_search", lambda: search)
        args = _parse("--tenant", TENANT, "--questions", _questions_file(tmp_path))
        assert memory_replay.cmd_memory_replay(args) == 0
        assert seen and all(seen)
        assert connection.read_only_active() is False


def test_cli_dispatches_memory_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    import robothor.cli as cli

    called: dict[str, Any] = {}

    def fake(args: Any) -> int:
        called["args"] = args
        return 0

    monkeypatch.setattr(memory_replay, "cmd_memory_replay", fake)
    from robothor.engine import instance_env

    monkeypatch.setattr(instance_env, "load_instance_env", lambda *a, **k: None)
    assert cli.main(["memory-replay", "--tenant", TENANT, "--from-chat"]) == 0
    assert called["args"].from_chat is True
