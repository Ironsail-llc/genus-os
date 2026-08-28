"""A safety control with no tracked source is one rebuild away from gone.

2026-08-27. Four drop-ins govern the local model server on this box --
concurrency, CPU quota, thermal containment and model residency -- and NONE had
a repo mirror, because ``dropin_conf_pairs`` globbed only
``robothor-engine.service.d``. The thermal one exists because this machine took
three thermal hard-cuts. The residency one exists because bundling residency
with thermal limits cost 15x on memory search. Both were learned the hard way
and both lived only in /etc.

Same class the thermal-guard adoption fixed and left half-open: the guard's own
script is mirrored and drift-checked, while the unit configuration bounding what
it guards was not.
"""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).parents[3]
MIRROR = ROOT / "infra" / "systemd" / "ollama.service.d"


def _guardrail_watch():
    spec = importlib.util.spec_from_file_location(
        "guardrail_watch_undertest", ROOT / "scripts" / "guardrail_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheLocalServerConfigIsTracked:
    def test_the_mirror_directory_exists(self):
        assert MIRROR.is_dir(), "the local model server's drop-ins are untracked"

    def test_thermal_containment_is_tracked(self):
        """Written after three thermal hard-cuts on this box."""
        conf = MIRROR / "thermal-limits.conf"
        assert conf.exists()
        assert "CPUQuota" in conf.read_text()

    def test_model_residency_is_tracked(self):
        """Bundling residency with thermal limits cost 15x on memory search."""
        conf = MIRROR / "zz-model-residency.conf"
        assert conf.exists()
        assert "OLLAMA_MAX_LOADED_MODELS" in conf.read_text()

    def test_the_concurrency_the_host_profile_reads_is_tracked(self):
        """HostProfile discovers inference slots from this value."""
        assert "OLLAMA_NUM_PARALLEL" in (MIRROR / "override.conf").read_text()


class TestDriftDetectionCoversThem:
    def test_the_pair_builder_covers_more_than_one_unit(self):
        units = {
            pathlib.Path(live).parent.name for live, _ in _guardrail_watch().dropin_conf_pairs()
        }
        assert "ollama.service.d" in units, (
            "the local model server's drop-ins are not drift-checked; a rebuilt "
            "box would silently lose thermal containment"
        )
        assert "robothor-engine.service.d" in units, "engine drop-ins must stay covered"

    def test_every_mirror_maps_to_its_own_unit_directory(self):
        for live, mirror in _guardrail_watch().dropin_conf_pairs():
            assert pathlib.Path(live).name == pathlib.Path(mirror).name
            assert pathlib.Path(live).parent.name == pathlib.Path(mirror).parent.name, (
                "a mirror pointing at the wrong unit would compare unrelated files "
                "and report drift that does not exist"
            )
