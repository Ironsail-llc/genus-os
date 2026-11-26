"""`memory-eval --world`: the flags parse, the world path is taken, and the
existing suite path is unchanged when the flag is absent."""

from __future__ import annotations

from typing import Any

import pytest

from robothor.cli import _build_parser
from robothor.cli import memory_eval as cli


def _parse(*argv: str):
    return _build_parser().parse_args(["memory-eval", *argv])


class TestFlags:
    def test_world_flags_parse(self):
        args = _parse(
            "--world", "--world-seed", "9", "--world-size", "small", "--split", "held_out"
        )
        assert args.world is True
        assert args.world_seed == 9
        assert args.world_size == "small"
        assert args.split == "held_out"

    def test_world_defaults(self):
        args = _parse("--world")
        assert args.world_size == "full"
        assert args.split == "dev"
        assert isinstance(args.world_seed, int)

    def test_without_world_the_flag_is_off(self):
        assert _parse().world is False

    @pytest.mark.parametrize(("flag", "value"), [("--world-size", "huge"), ("--split", "train")])
    def test_bad_choices_are_refused(self, flag, value):
        with pytest.raises(SystemExit):
            _parse("--world", flag, value)


class TestDispatch:
    def test_world_mode_runs_the_world_and_not_the_suite(self, monkeypatch, capsys):
        calls: dict[str, Any] = {}

        async def fake_run_world(seed, size, split, *, tenant_id, cleanup):
            calls.update(seed=seed, size=size, split=split, tenant=tenant_id, cleanup=cleanup)
            return {
                "suite_id": "memory-world",
                "total": 2,
                "passed": 2,
                "by_kind": {"multi_hop": {"passed": 2, "total": 2}},
                "unscored": {"abstention": 1},
                "cases": [],
            }

        async def no_suite(*_a, **_k):
            raise AssertionError("the 267-case suite must not run in world mode")

        monkeypatch.setattr(cli, "run_world", fake_run_world)
        monkeypatch.setattr(cli, "run_suite", no_suite)
        monkeypatch.setattr(cli, "preflight", lambda _t: None)

        rc = cli.cmd_memory_eval(_parse("--world", "--world-seed", "4", "--world-size", "small"))
        assert rc == 0
        assert calls == {
            "seed": 4,
            "size": "small",
            "split": "dev",
            # Never the nightly suite's tenant: the world has its own.
            "tenant": "memory-eval-world",
            "cleanup": True,
        }
        out = capsys.readouterr().out
        assert "multi_hop" in out
        assert "abstention" in out

    @staticmethod
    def _stub_world(monkeypatch, report):
        async def fake_run_world(seed, size, split, *, tenant_id, cleanup):
            return report

        monkeypatch.setattr(cli, "run_world", fake_run_world)
        monkeypatch.setattr(cli, "preflight", lambda _t: None)

    def test_world_mode_exits_zero_when_it_ran_whatever_the_pass_rate(self, monkeypatch):
        """No floor exists for the world yet; the suite's 0.90 would make every run exit 2."""
        report = {
            "suite_id": "w",
            "total": 10,
            "passed": 1,
            "by_kind": {"raw_detail": {"passed": 0, "total": 9}},
            "unscored": {},
            "cases": [],
        }
        self._stub_world(monkeypatch, report)
        assert cli.cmd_memory_eval(_parse("--world")) == 0

    def test_world_mode_with_nothing_scored_cannot_have_run(self, monkeypatch):
        report = {"suite_id": "w", "total": 0, "passed": 0, "by_kind": {}, "cases": []}
        self._stub_world(monkeypatch, report)
        assert cli.cmd_memory_eval(_parse("--world")) == 3

    def test_world_mode_refuses_the_suites_tenant(self, monkeypatch, capsys):
        self._stub_world(monkeypatch, {"total": 1, "passed": 1})
        assert cli.cmd_memory_eval(_parse("--world", "--tenant", "memory-eval")) == 3
        assert "nightly" in capsys.readouterr().out

    def test_suite_mode_is_unchanged(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_suite(path, tenant_id, cleanup):
            seen.update(path=str(path), tenant=tenant_id, cleanup=cleanup)
            return {
                "suite_id": "memory-recall-v1",
                "total": 1,
                "passed": 1,
                "by_kind": {},
                "cases": [],
            }

        async def no_world(*_a, **_k):
            raise AssertionError("world mode must not run without --world")

        monkeypatch.setattr(cli, "run_suite", fake_suite)
        monkeypatch.setattr(cli, "run_world", no_world)
        monkeypatch.setattr(cli, "preflight", lambda _t: None)

        assert cli.cmd_memory_eval(_parse()) == 0
        assert seen["path"].endswith("docs/benchmarks/memory/suite.yaml")
        assert seen["tenant"] == "memory-eval"
        assert seen["cleanup"] is True
