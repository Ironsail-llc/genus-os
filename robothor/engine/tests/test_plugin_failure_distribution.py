"""A refusal says which DISTRIBUTION it belongs to, because the loader knows.

``PluginFailure.name`` is the ENTRY-POINT name. ``genus-hostinfo`` publishes the
entry point ``hostinfo`` and contributes the handler ``host_state``: three
namespaces, none of them interchangeable. The reload response carried only the
entry-point name, so the Helm's plugin page had to *guess* which installed
distribution a refusal belonged to — it weighed the group, the ``enabled``
flag, a squashed prefix/suffix match on the distribution's own name and, as a
tiebreaker, ``manifest.declared`` (which holds contribution names and therefore
answers a different question entirely).

Every one of those inputs is available to the loader as a fact. ``ep.dist`` is
in hand at the moment the refusal is recorded, so the guess is replaced by the
answer, and the page's heuristic is deleted rather than improved.

``None`` is reserved for the one honest case: a distribution the metadata layer
cannot name. It must never mean "the loader did not bother".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.plugins import loader
from robothor.plugins.manifest import MANIFEST_NAME

_MANIFEST = (
    "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n  - web_fetch\n  - other\n"
)
_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}


class _Dist:
    def __init__(self, name="acme-tools", version="1.2.3", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _Nameless:
    """A distribution the metadata layer cannot name — the only honest None."""

    version = "0"
    files: tuple[str, ...] = ()
    name = ""
    metadata: dict[str, str] = {}

    def read_text(self, filename):
        return None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None, payload=None, raises=None):
        self.name, self.group = name, group
        self.dist = _Dist() if dist is None else dist
        self._payload = _PAYLOAD if payload is None else payload
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    from robothor.plugins import lockfile

    path = tmp_path / "plugins.lock"
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(path))
    lockfile.forget_warnings()
    return path


def _load(eps):
    return loader.load_plugins(entry_points=eps)


class TestTheDistributionIsCarried:
    def test_a_refused_entry_point_names_its_distribution(self, lock_path) -> None:
        bad = _EP(payload={"genus_contract_version": "0.1", "handlers": {"probe": 1}})
        result = _load([bad])
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.name == "probe", "the entry-point name is unchanged"
        assert failure.distribution == "acme-tools"

    def test_the_entry_point_name_and_the_distribution_are_different_namespaces(
        self, lock_path
    ) -> None:
        """The exact confusion this field exists to end."""
        dist = _Dist(name="genus-hostinfo")
        bad = _EP(name="hostinfo", dist=dist, payload={"genus_contract_version": "0.1"})
        failure = _load([bad]).failures[0]
        assert failure.name == "hostinfo"
        assert failure.distribution == "genus-hostinfo"

    @pytest.mark.parametrize(
        ("label", "ep"),
        [
            ("import error", _EP(raises=RuntimeError("boom"))),
            ("payload is not a dict", _EP(payload=["nope"])),
            ("contract mismatch", _EP(payload={"genus_contract_version": "0.1"})),
            ("no contributions", _EP(payload={"genus_contract_version": "1.0", "handlers": {}})),
            (
                "reserved name",
                _EP(payload={"genus_contract_version": "1.0", "handlers": {"web_fetch": 1}}),
            ),
            (
                "read_only is not a list",
                _EP(
                    payload={
                        "genus_contract_version": "1.0",
                        "handlers": {"probe": 1},
                        "read_only": "probe",
                    }
                ),
            ),
            (
                "read_only names a foreign tool",
                _EP(
                    payload={
                        "genus_contract_version": "1.0",
                        "handlers": {"probe": 1},
                        "read_only": ["not_ours"],
                    }
                ),
            ),
        ],
    )
    def test_every_kind_of_refusal_carries_it(self, label, ep, lock_path) -> None:
        result = _load([ep])
        assert result.failures, f"{label} produced no refusal to check"
        for failure in result.failures:
            assert failure.distribution == "acme-tools", label

    def test_a_name_already_claimed_carries_the_second_distribution(self, lock_path) -> None:
        first = _EP()
        second = _EP(name="probe2", dist=_Dist(name="other-tools"))
        result = _load([first, second])
        assert [f.distribution for f in result.failures] == ["other-tools"]

    def test_an_operator_disable_carries_it(self, lock_path) -> None:
        from robothor.plugins import lockfile

        with patch.object(loader, "_discover", lambda: [_EP()]):
            lockfile.sync()
        lockfile.set_enabled("acme-tools", False)
        failure = _load([_EP()]).failures[0]
        assert failure.reason == lockfile.DISABLED_REASON
        assert failure.distribution == "acme-tools"

    def test_a_missing_manifest_under_enforce_carries_it(self, lock_path, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
        ep = _EP(dist=_Dist(manifest=None))
        failure = _load([ep]).failures[0]
        assert "refused before import" in failure.reason
        assert failure.distribution == "acme-tools"

    def test_undeclared_surface_under_enforce_carries_it(self, lock_path, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_PLUGIN_MANIFEST_MODE", "enforce")
        ep = _EP(payload={"genus_contract_version": "1.0", "handlers": {"undeclared": 1}})
        failure = _load([ep]).failures[0]
        assert "undeclared" in failure.reason
        assert failure.distribution == "acme-tools"

    def test_a_distribution_nothing_can_name_is_none_not_empty(self, lock_path) -> None:
        """``None`` is "the loader genuinely cannot name it", never a default."""
        ep = _EP(dist=_Nameless(), payload={"genus_contract_version": "0.1"})
        failure = _load([ep]).failures[0]
        assert failure.distribution is None

    def test_the_field_defaults_to_none_so_old_constructors_still_work(self) -> None:
        """Three positional arguments is the shape every other caller uses."""
        failure = loader.PluginFailure("probe", "genus.tools", "because")
        assert failure.distribution is None
