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

So these tests pin the missing probe. Three properties do the work, and each
one is here because the first draft got it wrong:

* **It reaches the channel the incident happened on.** Asserted from the
  *builders'* output, never from the hook in isolation -- the first draft
  registered an agent hook that only ``build_warmth_preamble`` runs, so the
  section was absent from every Telegram turn, and the test that looked like
  it covered that asserted on a contextvar the hook never reads. A test that
  cannot fail certifies nothing.
* **It never states the opposite of the truth.** A model is called "the
  primary" only when it IS the configured primary. Runs that reached no model
  are not a model.
* **It degrades to "unknown", not to a fabricated number.** A failed probe
  must never be answered with this process's own clock.
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
START_PATCH = "robothor.engine.host_state._engine_start"
BLOCK_PATCH = "robothor.memory.blocks.read_block"
TRACKING_PATCH = "robothor.engine.tracking.get_schedule"

#: The REAL probe, captured at import -- before ``conftest.py``'s autouse
#: ``no_systemctl_in_engine_tests`` stub replaces the module attribute. The
#: handful of tests that exercise the probe's own argv need the real function;
#: everything else, in this file and every other engine test, gets the stub.
REAL_SYSTEMCTL_ACTIVE_ENTER = host_state._systemctl_active_enter


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


def _reach(*pairs: tuple[str | None, int]) -> list[dict[str, Any]]:
    return [{"model_used": name, "runs": runs} for name, runs in pairs]


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["systemctl"], returncode, stdout, "")


_ACTIVE = "ActiveEnterTimestamp=Sun 2026-09-13 08:29:43 UTC\nActiveState=active\n"


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
            return _completed(_ACTIVE)

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            assert REAL_SYSTEMCTL_ACTIVE_ENTER() is not None

        argv = " ".join(captured["cmd"])
        assert "ActiveEnterTimestamp" in argv
        assert "NRestarts" not in argv
        assert host_state.ENGINE_SERVICE_UNIT in captured["cmd"]

    def test_asks_systemd_for_utc_so_the_offset_is_not_re_derived(self) -> None:
        """The label used to be echoed while the offset was re-derived from the
        calling process's zone. Those disagree by an hour inside the DST
        fall-back window and by the whole offset after a host tz change. Asking
        for UTC removes the arithmetic instead of hedging it."""
        captured: dict[str, list[str]] = {}

        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return _completed(_ACTIVE)

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            probed = REAL_SYSTEMCTL_ACTIVE_ENTER()

        assert "--timestamp=utc" in captured["cmd"]
        assert probed is not None
        assert probed[0] == datetime(2026, 9, 13, 8, 29, 43, tzinfo=UTC)

    def test_retries_without_utc_flag_on_older_systemd(self) -> None:
        """``--timestamp=`` shipped in systemd v247. An older manager rejects
        the option with a non-zero exit; that must degrade to the legacy parse,
        not to 'unknown'."""
        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "--timestamp=utc" in cmd:
                return _completed("", returncode=1)
            return _completed(
                "ActiveEnterTimestamp=Sun 2026-09-13 08:29:43 UTC\nActiveState=active"
            )

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            probed = REAL_SYSTEMCTL_ACTIVE_ENTER()

        assert len(calls) == 2
        assert "--timestamp=utc" not in calls[1]
        assert probed is not None

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

    def test_systemd_host_with_a_failed_probe_never_reports_the_process_clock(
        self, main_config: AgentConfig
    ) -> None:
        """The bug this replaces: on a systemd host whose probe failed, the
        section reported *this process's* start as the engine's, under a header
        telling the model to trust it over memory. A sleeping systemctl made it
        say the engine had restarted one second ago.

        The process clock is the engine clock only where there is no unit to
        ask. On a systemd host a failed probe is "unknown", full stop.
        """
        process_started = datetime(2026, 9, 13, 14, 14, 59, tzinfo=UTC)
        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("robothor.engine.host_state._systemctl_active_enter", return_value=None),
            patch(
                "robothor.engine.host_state._process_start", return_value=process_started
            ) as proc,
            patch(DAL_PATCH, return_value=_reach(("model-a", 10))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "uptime: unknown" in section
        assert "up 1s" not in section
        assert proc.call_count == 0

    def test_systemctl_failure_yields_one_unknown_line(self, main_config: AgentConfig) -> None:
        def _boom(*_: object, **__: object) -> None:
            raise OSError("systemctl: command not found")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _boom),
            patch(DAL_PATCH, return_value=_reach(("model-a", 10))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "uptime: unknown" in section
        # One line, not a stack trace and not a missing section.
        assert sum("unknown" in line for line in section.splitlines()) == 1

    def test_systemctl_timeout_is_bounded(self) -> None:
        """A wedged systemd must cost the warmup a second, not the run. Two
        calls at most (the UTC probe plus the legacy retry), so the per-call
        ceiling is half the hook's one-second budget."""
        captured: dict[str, object] = {}

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.update(kwargs)
            return _completed(_ACTIVE)

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            REAL_SYSTEMCTL_ACTIVE_ENTER()

        assert captured["timeout"] == host_state.SYSTEMCTL_TIMEOUT_SECONDS
        assert host_state.SYSTEMCTL_TIMEOUT_SECONDS * 2 <= 1.0

    def test_unit_that_never_started_is_unknown(self) -> None:
        def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return _completed("ActiveEnterTimestamp=n/a\nActiveState=inactive\n")

        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", _fake_run),
        ):
            assert REAL_SYSTEMCTL_ACTIVE_ENTER() is None

    def test_nonexistent_unit_is_unknown(self) -> None:
        """``systemctl show`` for a unit that does not exist exits **0** with
        empty output. An instance whose unit is not named ``robothor-engine``
        would otherwise get the process clock, silently and permanently."""
        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch("subprocess.run", lambda *_a, **_k: _completed("")),
        ):
            assert REAL_SYSTEMCTL_ACTIVE_ENTER() is None

    def test_unit_with_no_active_state_is_unknown(self) -> None:
        """A timestamp without an ``ActiveState`` is not a running unit."""
        with (
            patch("robothor.engine.host_state._systemd_present", return_value=True),
            patch(
                "subprocess.run",
                lambda *_a, **_k: _completed("ActiveEnterTimestamp=Sun 2026-09-13 08:29:43 UTC\n"),
            ),
        ):
            assert REAL_SYSTEMCTL_ACTIVE_ENTER() is None


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
            patch(DAL_PATCH, return_value=_reach(("model-a", 310), ("model-b", 4), (None, 4))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        # 310 of 314 MODEL calls -- the 4 that reached no model are not a model,
        # and including them in the denominator understated the primary's reach.
        assert "98.7%" in section
        assert "310 of 314" in section
        assert "model-a" in section
        assert "model-b" in section
        assert "4 runs reached no model" in section

    def test_never_calls_a_non_primary_model_the_primary(self) -> None:
        """The case the section exists to settle -- "is the fleet on
        fallbacks?" -- was answered backwards: with the configured primary
        absent from the rows, the busiest model was renamed "the primary" and
        reported at 90%, under a header instructing the model to trust it over
        memory."""
        config = AgentConfig(id="main", name="Main", model_primary="openrouter/vendor/model-a")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-z", 90), ("model-y", 10))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "model-a" in section
        assert "0 of 100" in section
        assert "model-z" in section
        # The busiest model must not be described as the primary.
        assert "primary model-z" not in section
        assert "the primary model-a" not in section

    def test_no_model_runs_are_never_named_as_a_model(self) -> None:
        """``model_used IS NULL`` means the run reached no model at all. It
        used to be able to win both the primary slot and the runner-up slot."""
        config = AgentConfig(id="main", name="Main")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach((None, 200), ("model-a", 100))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "none" not in section.lower().replace("nonetheless", "")
        assert "200 runs reached no model" in section
        assert "model-a" in section

    def test_runs_that_reached_no_model_are_out_of_the_denominator(self) -> None:
        config = AgentConfig(id="main", name="Main", model_primary="model-a")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 90), (None, 10))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "90 of 90" in section
        assert "100.0%" in section
        assert "10 runs reached no model" in section

    def test_merges_rows_whose_short_id_is_the_same(self) -> None:
        """Two routes to one model were reported as 54.5% *and* as their own
        runner-up."""
        config = AgentConfig(id="main", name="Main", model_primary="openrouter/vendor/model-a")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("vendorX/model-a", 60), ("vendorY/model-a", 50))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "110 of 110" in section
        assert "100.0%" in section

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

    def test_only_no_model_runs_is_said_plainly(self, main_config: AgentConfig) -> None:
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach((None, 7))),
        ):
            section = host_state.host_state_context(main_config)
        assert section is not None
        assert "7 runs reached no model" in section
        assert "%" not in section.split("Model reach")[1]

    def test_agent_with_no_configured_primary_still_gets_an_honest_share(self) -> None:
        config = AgentConfig(id="main", name="Main")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-b", 9), ("model-a", 1))),
        ):
            section = host_state.host_state_context(config)
        assert section is not None
        assert "no primary model is configured" in section
        assert "model-b" in section
        assert "90.0%" in section


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

    def test_a_changed_primary_is_not_served_the_old_reach_line(self) -> None:
        """``chat_sessions.model_override`` can change main's effective primary
        between turns; the cache used to serve the previous turn's sentence."""
        first_cfg = AgentConfig(id="main", name="Main", model_primary="model-a")
        second_cfg = AgentConfig(id="main", name="Main", model_primary="model-b")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 9), ("model-b", 1))),
        ):
            first = host_state.host_state_context(first_cfg)
            second = host_state.host_state_context(second_cfg)
        assert first is not None
        assert second is not None
        assert first != second
        assert "model-b" in second.split("Model reach")[1]


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

    def test_wants_host_state_by_agent_id_alone(self) -> None:
        """The interactive builder knows the agent id, not its config."""
        assert host_state.wants_host_state("main") is True
        assert host_state.wants_host_state("email-responder") is False


class TestBothPreamblesRenderIt:
    """Asserted from the BUILDERS, because the hook in isolation proved nothing.

    The first draft registered an agent hook, and ``_run_agent_context_hooks``
    is called from ``build_warmth_preamble`` alone. Telegram, webchat and
    channel-event runs go to ``build_interactive_preamble`` -- the path the
    operator was on when the stale fact was asserted -- and rendered no section
    at all.
    """

    def test_cron_preamble_contains_the_section(self, tmp_path: Any) -> None:
        from robothor.engine.warmup import build_warmth_preamble

        config = AgentConfig(id="main", name="Main", model_primary="model-a")
        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value=None),
        ):
            preamble, _ = build_warmth_preamble(config, tmp_path)
        assert "LIVE ENGINE STATE" in preamble

    def test_interactive_preamble_contains_the_section(self) -> None:
        from robothor.engine.warmup import build_interactive_preamble

        with (
            patch(START_PATCH, return_value=None),
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))),
            patch(BLOCK_PATCH, return_value=None),
        ):
            preamble = build_interactive_preamble("main", include_blocks=False)
        assert "LIVE ENGINE STATE" in preamble

    def test_interactive_preamble_omits_it_for_a_worker(self) -> None:
        from robothor.engine.warmup import build_interactive_preamble

        with (
            patch(START_PATCH, return_value=None) as start,
            patch(DAL_PATCH, return_value=_reach(("model-a", 3))) as dal,
            patch(BLOCK_PATCH, return_value=None),
        ):
            preamble = build_interactive_preamble("email-responder", include_blocks=False)
        assert "LIVE ENGINE STATE" not in preamble
        assert start.call_count == 0
        assert dal.call_count == 0

    def test_a_failing_section_does_not_break_the_interactive_preamble(self) -> None:
        from robothor.engine.warmup import build_interactive_preamble

        with (
            patch(
                "robothor.engine.host_state.host_state_section",
                side_effect=RuntimeError("probe exploded"),
            ),
            patch(BLOCK_PATCH, return_value=None),
        ):
            preamble = build_interactive_preamble("main", sender_name="Alice", include_blocks=False)
        assert "Alice" in preamble


class TestCronWarmupIsNotGatedOnAWarmupBlock:
    """A heartbeat agent that declares no ``warmup:`` block built no preamble
    at all, so targeting it bought nothing and both docs were wrong."""

    def test_heartbeat_agent_without_a_warmup_block_still_warms(self) -> None:
        from robothor.engine.warmup import wants_cron_warmup

        config = AgentConfig(
            id="ops-watch", name="Ops Watch", heartbeat=HeartbeatConfig(cron_expr="*/30 * * * *")
        )
        assert not config.warmup_memory_blocks
        assert not config.warmup_context_files
        assert not config.warmup_peer_agents
        assert wants_cron_warmup(config) is True

    def test_operator_agent_without_a_warmup_block_still_warms(self) -> None:
        from robothor.engine.warmup import wants_cron_warmup

        assert wants_cron_warmup(AgentConfig(id="main", name="Main")) is True

    def test_plain_worker_without_a_warmup_block_still_does_not(self) -> None:
        from robothor.engine.warmup import wants_cron_warmup

        assert wants_cron_warmup(AgentConfig(id="worker", name="Worker")) is False

    def test_declared_warmup_still_warms(self) -> None:
        from robothor.engine.warmup import wants_cron_warmup

        config = AgentConfig(id="worker", name="Worker", warmup_memory_blocks=["ops"])
        assert wants_cron_warmup(config) is True

    def test_the_runner_uses_that_predicate(self) -> None:
        """Pinning the call site: the disjunction used to be inlined in
        ``execute`` where nothing could reach it."""
        import inspect

        from robothor.engine import runner

        source = inspect.getsource(runner.AgentRunner.execute)
        assert "wants_cron_warmup" in source


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
