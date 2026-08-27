"""The first plugin that actually exists.

Before this, the seam declared ten entry-point groups and had ZERO installed
payload — `load_plugins()` returned `loaded=[] tools=0`, and `genusos` was not
resolvable on PyPI. A socket nobody has plugged anything into is not an
extensibility story, and it was this platform's weakest competitive axis.

This distribution is also the end-to-end test of the pre-import manifest gate:
a real pip install, a real `genus-plugin.yaml` in the distribution's file list,
and refusal-before-import that a fabricated entry point cannot prove.
"""

from __future__ import annotations

import asyncio

import genus_hostinfo
from genus_hostinfo import PLUGIN, host_state


class TestTheContract:
    def test_it_declares_the_contract_version_the_engine_speaks(self):
        assert PLUGIN["genus_contract_version"] == "1.0"

    def test_every_declared_handler_exists(self):
        for name, fn in PLUGIN["handlers"].items():
            assert callable(fn), name

    def test_the_manifest_declares_exactly_what_the_payload_offers(self):
        """A manifest that omits a contribution gets it refused; one that
        over-declares is a lie about intent."""
        from pathlib import Path

        import yaml

        manifest = yaml.safe_load(
            (Path(genus_hostinfo.__file__).parent / "genus-plugin.yaml").read_text()
        )
        assert set(manifest["handlers"]) == set(PLUGIN["handlers"])
        assert set(manifest["schemas"]) == set(PLUGIN["schemas"])

    def test_the_schema_names_match_the_handlers(self):
        assert set(PLUGIN["schemas"]) == set(PLUGIN["handlers"])

    def test_it_declares_itself_read_only(self):
        """Reporting host state changes nothing; saying so keeps it usable by
        agents under a restrictive tool policy."""
        assert set(PLUGIN["read_only"]) == set(PLUGIN["handlers"])


class TestItAnswersHonestly:
    def test_it_returns_the_expected_shape(self):
        state = asyncio.run(host_state())
        assert set(state) == {
            "cpu_zone_temps_c",
            "cpu_max_temp_c",
            "gpu",
            "available_memory_gb",
        }

    def test_unknown_is_none_never_zero(self, monkeypatch):
        """A laptop with no GPU, or a container with no /sys, must be told it is
        unknown. Substituting 0 would read as 'stone cold' and 'no memory'."""
        from pathlib import Path

        monkeypatch.setattr(genus_hostinfo, "_THERMAL", Path("/nonexistent"))
        monkeypatch.setattr(genus_hostinfo, "_MEMINFO", Path("/nonexistent"))
        monkeypatch.setattr(genus_hostinfo.shutil, "which", lambda _: None)

        state = asyncio.run(host_state())
        assert state["cpu_zone_temps_c"] is None
        assert state["cpu_max_temp_c"] is None
        assert state["gpu"] is None
        assert state["available_memory_gb"] is None

    def test_it_does_not_report_vram(self):
        """nvidia-smi returns [N/A] for memory on unified-memory parts (GB10);
        a caller trusting it would read nothing as zero."""
        state = asyncio.run(host_state())
        if state["gpu"]:
            assert "memory" not in " ".join(state["gpu"]).lower()
