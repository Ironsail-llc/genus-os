"""An agent must not learn the engine's state from semantic memory.

2026-09-13: main told the operator the engine "hasn't been restarted since
Sep 3" and that "51-56% of runs never reach the primary". Both were false at
the moment they were said -- the engine had restarted at 08:29 that morning
(fifteen starts in thirty hours) and 98.8% of that day's runs landed on the
primary model. Neither claim came from a probe. They came from undated
``memory_facts`` rows that were true when written, carried no as-of date in
their own text, and were recalled as present-tense truth.

Nothing could have corrected them: ``build_warmth_preamble`` had no
engine-health section, the registered context hooks were date, travel,
weather, git and thread-pool only, and the single ``systemctl`` call in the
whole engine reads ``ollama.service``'s environment.

So these tests pin the missing probe. The section is LIVE by construction --
it says so in its own text -- and it degrades to one "unknown" line rather
than an exception, because a warmup section that can raise is a warmup
section that gets reverted.
"""

from __future__ import annotations

import socket
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from robothor.engine import host_state
from robothor.engine.models import AgentConfig, HeartbeatConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

# Patch targets -- the module resolves both lazily so warmup stays importable
# without a database driver.
DAL_PATCH = "robothor.crm.dal.get_model_reach_24h"
SYSTEMCTL_PATCH = "robothor.engine.host_state._systemctl_active_enter"
START_PATCH = "robothor.engine.host_state._engine_start"


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    host_state.reset_host_state_cache()
    yield
    host_state.reset_host_state_cache()


@pytest.fixture
def main_config() -> AgentConfig:
    return AgentConfig(id="main", name="Main", model_primary="openrouter/vendor/model-a")


@pytest.fixture
def worker_config() -> AgentConfig:
    return AgentConfig(id="email-responder", name="Email Responder")


@pytest.fixture
def heartbeat_config() -> AgentConfig:
    return AgentConfig(
        id="ops-watch",
        name="Ops Watch",
        heartbeat=HeartbeatConfig(cron_expr="*/30 * * * *"),
    )


def _reach(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"model_used": name, "runs": runs} for name, runs in pairs]


class TestEngineUptime:
    """``ActiveEnterTimestamp``, rendered as an age the model cannot mis-subtract."""

    def test_renders_age_from_systemctl_timestamp(self, main_config: AgentConfig) -> None:
        started = datetime(2026, 9, 13, 8, 29, 43, tzinfo=UTC)
        now = datetime(2026, 9, 13, 14, 15, 0, tzinfo=UTC)
        with (
            patch(START_PATCH, return_value=(started, "2026-09-13 08:29 UTC")),
            patch(DAL_PATCH, return_value=_reach(("model-a", 310))),
            patch.object(host_state, "_now", return_value=now),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "up 5h 45m" in section
        assert "2026-09-13 08:29 UTC" in section

    def test_reads_active_enter_timestamp_not_nrestarts(self) -> None:
        """``NRestarts`` resets on a manual or deploy restart -- it reported 0
        on a host that had started fifteen times in thirty hours. It is the
        wrong probe and must not appear in the command line."""
        captured: dict[str, list[str]] = {}

        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "Sun 2026-09-13 08:29:43 UTC\n", "")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            host_state._systemctl_active_enter()

        assert "ActiveEnterTimestamp" in " ".join(captured["cmd"])
        assert "NRestarts" not in " ".join(captured["cmd"])
        assert host_state.ENGINE_SERVICE_UNIT in captured["cmd"]

    def test_falls_back_to_process_start_without_systemd(self, main_config: AgentConfig) -> None:
        started = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)
        now = datetime(2026, 9, 13, 9, 30, 0, tzinfo=UTC)
        with (
            patch("robothor.engine.host_state._systemd_present", return_value=False),
            patch("robothor.engine.host_state._process_start", return_value=started),
            patch(DAL_PATCH, return_value=_reach(("model-a", 10))),
            patch.object(host_state, "_now", return_value=now),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "up 1h 30m" in section

    def test_systemctl_failure_yields_one_unknown_line(self, main_config: AgentConfig) -> None:
        def _boom(*_: object, **__: object) -> None:
            raise OSError("systemctl: command not found")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _boom),
            patch("robothor.engine.host_state._process_start", return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 10))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "uptime: unknown" in section
        # One line, not a stack trace and not a missing section.
        assert sum("unknown" in line for line in section.splitlines()) == 1

    def test_systemctl_timeout_is_bounded(self) -> None:
        """A wedged systemd must cost the warmup a second, not the run."""
        captured: dict[str, object] = {}

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "n/a\n", "")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            host_state._systemctl_active_enter()

        assert captured["timeout"] == host_state.SYSTEMCTL_TIMEOUT_SECONDS
        assert host_state.SYSTEMCTL_TIMEOUT_SECONDS <= 1.0

    def test_unit_that_never_started_is_unknown(self) -> None:
        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, "n/a\n", "")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            assert host_state._systemctl_active_enter() is None


class TestPlatformVersion:
    def test_reports_the_running_package_version(self, main_config: AgentConfig) -> None:
        from robothor import __version__

        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 4))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert __version__ in section


class TestModelReach:
    def test_names_primary_share_and_top_fallback(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(
                DAL_PATCH,
                return_value=_reach(("model-a", 310), ("model-b", 4), ("none", 4)),
            ),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "97.5%" in section
        assert "model-a" in section
        assert "model-b" in section
        assert "4" in section

    def test_single_query_only(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 5))) as dal,
        ):
            host_state.host_state_context(main_config)
        assert dal.call_count == 1

    def test_query_is_tenant_scoped(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 5))) as dal,
            patch("robothor.engine.host_state._tenant_id", return_value="tenant-x"),
        ):
            host_state.host_state_context(main_config)
        assert dal.call_args.kwargs["tenant_id"] == "tenant-x"

    def test_db_failure_yields_one_unknown_line(self, main_config: AgentConfig) -> None:
        started = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)
        with (
            patch(START_PATCH, return_value=(started, "2026-09-13 08:00 UTC")),
            patch(DAL_PATCH, side_effect=RuntimeError("connection refused")),
            patch.object(
                host_state, "_now", return_value=datetime(2026, 9, 13, 9, 0, 0, tzinfo=UTC)
            ),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "Model reach" in section
        assert sum("unknown" in line for line in section.splitlines()) == 1

    def test_no_runs_is_said_plainly(self, main_config: AgentConfig) -> None:
        with patch(START_PATCH, return_value=None), patch(DAL_PATCH, return_value=[]):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "no runs recorded" in section

    def test_falls_back_to_busiest_model_when_primary_unconfigured(
        self, worker_config: AgentConfig
    ) -> None:
        """An agent with no ``model_primary`` still gets an honest share."""
        config = AgentConfig(id="main", name="Main")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-b", 9), ("model-a", 1))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "90.0%" in section
        assert "model-b" in section


class TestLiveness:
    def test_section_declares_itself_live(self, main_config: AgentConfig) -> None:
        """The whole point: the model must not weigh a nine-day-old recalled
        fact against this."""
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "as of now" in section.lower()

    def test_unknown_lines_are_still_as_of_now(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, side_effect=RuntimeError("down")),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "as of now" in section.lower()


class TestCache:
    def test_probes_once_per_minute(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None) as start,
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))) as dal,
        ):
            first = host_state.host_state_context(main_config)
            second = host_state.host_state_context(main_config)
        assert first == second
        assert start.call_count == 1
        assert dal.call_count == 1

    def test_reprobes_after_the_ttl(self, main_config: AgentConfig) -> None:
        clock = [1000.0]
        with (
            patch(START_PATCH, return_value=None) as start,
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
            patch.object(host_state.time, "monotonic", lambda: clock[0]),
        ):
            host_state.host_state_context(main_config)
            clock[0] += host_state.HOST_STATE_CACHE_TTL_SECONDS + 1
            host_state.host_state_context(main_config)
        assert start.call_count == 2
        assert host_state.HOST_STATE_CACHE_TTL_SECONDS == 60


class TestTargeting:
    def test_operator_agent_gets_the_section(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
        ):
            assert host_state.host_state_context(main_config) is not None

    def test_heartbeat_agent_gets_the_section(self, heartbeat_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
        ):
            assert host_state.host_state_context(heartbeat_config) is not None

    def test_plain_worker_does_not(self, worker_config: AgentConfig) -> None:
        """Workers report through CRM tasks; a per-worker probe would be one
        systemctl call and one aggregate query per fleet run for context the
        worker cannot act on."""
        with (
            patch(START_PATCH, return_value=None) as start,
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))) as dal,
        ):
            assert host_state.host_state_context(worker_config) is None
        assert start.call_count == 0
        assert dal.call_count == 0

    def test_registered_as_an_agent_context_hook(self) -> None:
        """Opt-out is the existing mechanism: an instance that does not want
        this drops the registration, exactly as with every other hook."""
        from robothor.engine.warmup import _AGENT_CONTEXT_HOOKS

        assert host_state.host_state_context in _AGENT_CONTEXT_HOOKS

    def test_renders_on_a_cron_warmup_not_only_interactive(self, main_config: AgentConfig) -> None:
        """A section that only renders interactively goes missing from exactly
        the runs that need it -- the heartbeat is where the stale fact was
        re-asserted."""
        from robothor.engine.warmup import set_warmup_kind

        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
        ):
            with set_warmup_kind("cron"):
                cron = host_state.host_state_context(main_config)
            host_state.reset_host_state_cache()
            with set_warmup_kind("interactive"):
                interactive = host_state.host_state_context(main_config)
        assert cron is not None
        assert interactive is not None


class TestNoInstanceData:
    def test_section_names_no_host_no_chat_and_no_instance(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3), ("model-b", 1))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        lowered = section.lower()
        assert socket.gethostname().lower() not in lowered
        for forbidden in ("chat_id", "chat id", "@", "robothor-primary", "/home/"):
            assert forbidden not in lowered

    def test_module_carries_no_instance_identifiers(self) -> None:
        from pathlib import Path

        source = (Path(host_state.__file__)).read_text().lower()
        for forbidden in ("@gmail", "@valhalla", "thinkstation", "/home/"):
            assert forbidden not in source


class TestFormatAge:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (45, "45s"),
            (90, "1m"),
            (3600, "1h 0m"),
            (20_700, "5h 45m"),
            (90_000, "1d 1h"),
        ],
    )
    def test_ages_read_as_words(self, seconds: int, expected: str) -> None:
        assert host_state._format_age(float(seconds)) == expected
