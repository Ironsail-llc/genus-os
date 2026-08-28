"""The capability probe must describe the box it is on, not the box it was written on.

2026-08-27. The local-tier outage exposed that every number describing this
machine's capacity was hardcoded somewhere: ``ttft_hint_ms=9000`` sat in the
model registry commented "dense 27B on GB10", ``keep_alive`` was sized for
"5.8GiB of 54GiB free", and the inference-slot count existed only as
``OLLAMA_NUM_PARALLEL`` in a systemd drop-in no Python ever read.

A platform that ships to other people cannot carry this box's numbers. So the
profile is *discovered*, every field records WHERE its value came from, and an
unknown is ``None`` — never a guess. These tests are written to fail on a
laptop with no GPU, no sensors and no systemd, because that is the host the
platform has to work on.
"""

import robothor.engine.host_profile as hp


class TestUnknownIsNeverAGuess:
    def test_a_bare_host_yields_a_usable_profile_and_never_raises(self, monkeypatch):
        """No GPU, no sensors, no systemd, no /proc — still a valid profile."""
        monkeypatch.setattr(hp, "_MEMINFO_PATH", "/nonexistent/meminfo")
        monkeypatch.setattr(hp, "_NVIDIA_VERSION_PATH", "/nonexistent/nvidia")
        monkeypatch.setattr(hp, "_ROCM_PATH", "/nonexistent/kfd")
        monkeypatch.setattr(hp, "_THERMAL_ROOT", "/nonexistent")
        monkeypatch.setattr(hp, "_systemctl_ollama_environment", lambda: None)
        monkeypatch.delenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)

        profile = hp.detect_host_profile()

        assert profile.total_memory_gb.value is None
        assert profile.available_memory_gb.value is None
        assert profile.thermal_sensors.value is False
        # The one field that must always be usable: you cannot run zero models.
        assert profile.inference_slots.value == hp.DEFAULT_INFERENCE_SLOTS
        assert profile.inference_slots.source == hp.DEFAULT

    def test_an_unknown_memory_reading_is_none_not_zero(self, monkeypatch):
        """Zero free memory and unknown free memory must not be the same value."""
        monkeypatch.setattr(hp, "_MEMINFO_PATH", "/nonexistent/meminfo")
        assert hp._available_memory_gb() is None

    def test_garbage_in_meminfo_is_unknown_rather_than_an_exception(self, tmp_path, monkeypatch):
        bad = tmp_path / "meminfo"
        bad.write_text("MemAvailable:  not-a-number kB\n")
        monkeypatch.setattr(hp, "_MEMINFO_PATH", str(bad))
        assert hp._available_memory_gb() is None


class TestExplicitConfigBeatsAProbe:
    def test_operator_setting_wins_over_the_ollama_env(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "7")
        monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
        slots, source = hp.detect_inference_slots()
        assert slots == 7
        assert source == hp.CONFIGURED

    def test_the_ollama_env_wins_over_the_conservative_default(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
        slots, source = hp.detect_inference_slots()
        assert slots == 2
        assert source == hp.PROBED

    def test_systemd_is_consulted_only_when_the_env_is_silent(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
        monkeypatch.setattr(
            hp, "_systemctl_ollama_environment", lambda: "OLLAMA_NUM_PARALLEL=3 OTHER=x"
        )
        slots, source = hp.detect_inference_slots()
        assert slots == 3
        assert source == hp.PROBED

    def test_a_nonsense_setting_falls_back_rather_than_crashing_the_engine(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "banana")
        monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
        monkeypatch.setattr(hp, "_systemctl_ollama_environment", lambda: None)
        slots, source = hp.detect_inference_slots()
        assert slots == hp.DEFAULT_INFERENCE_SLOTS
        assert source == hp.DEFAULT

    def test_a_zero_or_negative_slot_count_is_refused(self, monkeypatch):
        """Zero slots would deadlock the fleet — it is nonsense, not a policy."""
        monkeypatch.setenv("ROBOTHOR_LOCAL_MAX_CONCURRENT", "0")
        monkeypatch.delenv("OLLAMA_NUM_PARALLEL", raising=False)
        monkeypatch.setattr(hp, "_systemctl_ollama_environment", lambda: None)
        slots, _ = hp.detect_inference_slots()
        assert slots == hp.DEFAULT_INFERENCE_SLOTS


class TestSystemctlProbeIsBestEffort:
    def test_a_missing_systemctl_is_not_an_error(self, monkeypatch):
        """Non-systemd hosts (macOS, containers) must not take an exception path."""
        monkeypatch.setattr(hp.shutil, "which", lambda _: None)
        assert hp._systemctl_ollama_environment() is None

    def test_a_failing_systemctl_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(hp.shutil, "which", lambda _: "/usr/bin/systemctl")

        def boom(*a, **k):
            raise OSError("no such service")

        monkeypatch.setattr(hp.subprocess, "run", boom)
        assert hp._systemctl_ollama_environment() is None


class TestNothingIsKeyedToThisMachine:
    def test_the_conservative_default_is_one_not_this_box_s_two(self):
        """This host serves 2. A platform default of 2 would be tuning to it."""
        assert hp.DEFAULT_INFERENCE_SLOTS == 1

    def test_no_vendor_hardware_model_is_named_in_the_source(self):
        """A probe that mentions the box it was written on is a hardcoded profile."""
        import pathlib

        source = pathlib.Path(hp.__file__).read_text().lower()
        for banned in ("gb10", "thinkstation", "grace", "5.8gib", "54gib"):
            assert banned not in source, f"{banned!r} names this machine"

    def test_every_reading_records_where_it_came_from(self, monkeypatch):
        monkeypatch.setattr(hp, "_systemctl_ollama_environment", lambda: None)
        profile = hp.detect_host_profile()
        for name, reading in profile.readings().items():
            assert reading.source in (hp.PROBED, hp.CONFIGURED, hp.DEFAULT), name
