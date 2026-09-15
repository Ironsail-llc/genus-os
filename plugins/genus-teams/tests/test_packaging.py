"""The distribution itself: what it declares, what the scanner makes of it, and
what installing it does NOT do.

Three properties, and each one has a specific failure behind it.

**The manifest matches the payload.** ``genus plugin install`` compares the
wheel's actual entry points against ``genus-plugin.yaml`` *before* importing
anything, so a manifest that drifts from the code is refused at install time on
somebody else's box — which is a worse place to find out than here.

**The platform's own scanner passes it, at ``review``.** Not ``safe``: this
package reaches off the box (``httpx``) and contributes to an ambient group (a
channel runs without a tool call), and both are things an operator should look
at. Not ``blocked`` either — a channel plugin that the platform's installer
refuses is a channel plugin nobody can install, and that is the whole
distribution being wrong rather than a finding.

**Installing is not arming.** The rule the channel registry exists to enforce,
proved against the real ``PLUGIN`` object rather than a stand-in.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import genus_teams
import pytest
import yaml
from genus_teams import CHANNEL, PLUGIN

ROOT = Path(genus_teams.__file__).resolve().parent
DISTRIBUTION = ROOT.parent


def _manifest() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "genus-plugin.yaml").read_text(encoding="utf-8"))


def _pyproject() -> str:
    return (DISTRIBUTION / "pyproject.toml").read_text(encoding="utf-8")


class TestTheContract:
    def test_it_declares_the_contract_version_the_engine_speaks(self):
        assert PLUGIN["genus_contract_version"] == "1.0"

    def test_the_manifest_declares_exactly_what_the_payload_offers(self):
        manifest = _manifest()
        assert set(manifest["channels"]) == set(PLUGIN["channels"])
        assert set(manifest["checks"]) == set(PLUGIN["checks"])

    def test_the_manifest_declares_the_entry_points_the_distribution_publishes(self):
        """Declaring these is what lets the installer compare by NAME rather
        than only by group. Omitting them caps the verdict at review for a
        reason that has nothing to do with this package's actual behaviour."""
        manifest = _manifest()
        pyproject = _pyproject()
        for group, names in manifest["entry_points"].items():
            assert f'[project.entry-points."{group}"]' in pyproject, group
            for name in names:
                assert f"\n{name} = " in pyproject, (group, name)

    def test_the_channel_is_the_object_the_entry_point_names(self):
        assert CHANNEL is PLUGIN["channels"]["teams"]
        assert 'teams = "genus_teams:CHANNEL"' in _pyproject()

    def test_it_is_not_declared_as_anything_it_does_not_contribute(self):
        """An over-declared manifest is a lie about intent, and the installer
        treats it as one."""
        manifest = _manifest()
        assert set(manifest) <= {
            "name",
            "contract_version",
            "channels",
            "checks",
            "entry_points",
        }


class TestTheDoctorChecks:
    def test_every_check_id_carries_the_distributions_name(self):
        """The doctor's registry refuses a plugin whose ids do not, so that a
        package cannot publish a check that reads like a platform one."""
        for check_id, check in PLUGIN["checks"].items():
            assert check_id.startswith("genus_teams_")
            assert check.id == check_id

    def test_no_check_shadows_a_builtin(self):
        from robothor.doctor.registry import builtin_ids

        assert set(PLUGIN["checks"]).isdisjoint(set(builtin_ids()))

    @pytest.mark.asyncio
    async def test_an_unconfigured_instance_is_a_skip_and_not_a_failure(self, monkeypatch):
        """An instance that never wanted Teams has not failed a check it did not
        ask for — and a skip names the reason, so it does not read as a pass."""
        from genus_teams import credentials as credentials_module

        monkeypatch.setattr(
            credentials_module,
            "teams_credentials",
            lambda **_kw: credentials_module.TeamsCredentials(),
        )
        result = await PLUGIN["checks"]["genus_teams_credentials"].run(None)
        assert result.status == "skip"
        assert "genus channel add teams" in result.detail

    @pytest.mark.asyncio
    async def test_a_half_configured_instance_fails_and_names_what_is_missing(self, monkeypatch):
        from genus_teams import credentials as credentials_module

        monkeypatch.setattr(
            credentials_module,
            "teams_credentials",
            lambda **_kw: credentials_module.TeamsCredentials(app_id="00000000-0000"),
        )
        result = await PLUGIN["checks"]["genus_teams_credentials"].run(None)
        assert result.status == "fail"
        assert "ROBOTHOR_TEAMS_APP_PASSWORD" in result.detail

    @pytest.mark.asyncio
    async def test_no_check_detail_carries_a_credential(self, monkeypatch):
        from genus_teams import credentials as credentials_module

        secret = "not-a-real-client-secret-9999"
        monkeypatch.setattr(
            credentials_module,
            "teams_credentials",
            lambda **_kw: credentials_module.TeamsCredentials(
                app_id="00000000-0000", app_password=secret, app_id_source="vault"
            ),
        )
        result = await PLUGIN["checks"]["genus_teams_credentials"].run(None)
        assert secret not in result.detail

    @pytest.mark.asyncio
    async def test_an_unarmed_channel_is_a_skip_that_names_the_variable(self, monkeypatch):
        from robothor.settings import reset_settings

        monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
        reset_settings()
        result = await PLUGIN["checks"]["genus_teams_endpoint"].run(None)
        assert result.status == "skip"
        assert "ROBOTHOR_CHANNELS" in result.detail
        reset_settings()


class TestMountedMeansMounted:
    """`endpoint_mounted` and the endpoint doctor check both used to read
    `inbound_router is not None`, which only proves the object was built. The
    check exists to catch "armed nowhere while the outbound side looks healthy",
    and it could not see the layer it was written to watch: a router the engine
    REFUSED (a route outside this channel's path, an include that raised, no
    runner to bind to) still reported a mounted endpoint."""

    @pytest.fixture(autouse=True)
    def _armed_and_unmounted(self, monkeypatch):
        from robothor.engine.channels.routers import reset_mounted
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "teams")
        reset_settings()
        reset_mounted()
        yield
        reset_mounted()
        reset_settings()

    @pytest.mark.asyncio
    async def test_a_router_that_was_never_mounted_is_reported_as_a_failure(self):
        result = await PLUGIN["checks"]["genus_teams_endpoint"].run(None)
        assert result.status == "fail"
        assert "not mounted" in result.detail

    @pytest.mark.asyncio
    async def test_a_mounted_router_passes(self):
        from robothor.engine.channels.routers import _mounted

        _mounted.add("teams")
        result = await PLUGIN["checks"]["genus_teams_endpoint"].run(None)
        assert result.status == "pass"

    @pytest.mark.asyncio
    async def test_health_reports_the_mounting_and_not_the_object(self, monkeypatch):
        from genus_teams import credentials as credentials_module
        from genus_teams.channel import TeamsChannel

        monkeypatch.setattr(
            credentials_module,
            "teams_credentials",
            lambda **_kw: credentials_module.TeamsCredentials(),
        )
        channel = TeamsChannel()
        # The object exists the moment anything asks for it.
        assert channel.inbound_router is not None
        report = await channel.health()
        assert report["endpoint_mounted"] is False, (
            "health reported an endpoint for a router the engine never mounted"
        )


class TestInstallingIsNotArming:
    @pytest.fixture
    def installed(self, monkeypatch):
        """The REAL plugin payload, discovered as an installed distribution."""
        from robothor.engine.channels import reset_channels
        from robothor.plugins import loader, reload_plugins

        class _EntryPoint:
            group = "genus.channels"
            name = "teams"

            def load(self) -> dict:
                return PLUGIN

        monkeypatch.setattr(loader, "_discover", lambda: [_EntryPoint()])
        reload_plugins()
        reset_channels()
        yield
        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        reset_channels()

    def test_an_installed_but_unnamed_teams_channel_does_not_resolve(self, installed, monkeypatch):
        from robothor.engine.channels import get_channel
        from robothor.settings import reset_settings

        monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
        reset_settings()
        assert get_channel("teams") is None, "installing the package made it the delivery surface"

    def test_naming_it_arms_it(self, installed, monkeypatch):
        from robothor.engine.channels import get_channel, reset_channels
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "teams")
        reset_settings()
        reset_channels()
        resolved = get_channel("teams")
        assert resolved is CHANNEL

    def test_it_could_not_claim_a_builtin_name_even_if_it_tried(self):
        from robothor.engine.channels.registry import BUILTIN_CHANNELS

        assert set(PLUGIN["channels"]).isdisjoint(BUILTIN_CHANNELS)

    def test_it_appears_in_the_listing_only_once_armed(self, installed, monkeypatch):
        """``list_channels`` is what the Channels page renders. An
        installed-but-unarmed channel appearing there would tell an operator a
        manifest could name it."""
        from robothor.engine.channels import list_channels, reset_channels
        from robothor.settings import reset_settings

        monkeypatch.delenv("ROBOTHOR_CHANNELS", raising=False)
        reset_settings()
        reset_channels()
        assert "teams" not in list_channels()

        monkeypatch.setenv("ROBOTHOR_CHANNELS", "teams")
        reset_settings()
        reset_channels()
        assert "teams" in list_channels()


def _build_wheel(destination: Path) -> tuple[Path, bytes]:
    """A wheel of the REAL package, built in a temporary directory.

    Assembled with ``zipfile`` rather than by invoking a build backend: a wheel
    is a zip, the point is to put *this* source through the platform's scanner,
    and shelling out to a builder would make the test depend on what is
    installed in the environment running it.
    """
    path = destination / "genus_teams-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for source in sorted(ROOT.glob("*.py")):
            archive.writestr(f"genus_teams/{source.name}", source.read_text(encoding="utf-8"))
        archive.writestr(
            "genus_teams/genus-plugin.yaml",
            (ROOT / "genus-plugin.yaml").read_text(encoding="utf-8"),
        )
        archive.writestr(
            "genus_teams-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: genus-teams\nVersion: 0.1.0\n"
            "Summary: Microsoft Teams as a Genus OS channel\n",
        )
        archive.writestr(
            "genus_teams-0.1.0.dist-info/entry_points.txt",
            "[genus.channels]\nteams = genus_teams:CHANNEL\n\n"
            "[genus.doctor]\nteams = genus_teams:PLUGIN\n",
        )
    return path, path.read_bytes()


class TestThePlatformsOwnScanner:
    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        """A workspace and a lockfile of this test's own, never the operator's."""
        from robothor.plugins import lockfile
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        reset_settings()
        lockfile.forget_warnings()
        yield tmp_path
        reset_settings()

    def test_the_scanner_says_review_and_names_why(self, isolated):
        from robothor.plugins import scan, wheel

        path, _data = _build_wheel(isolated)
        contents = wheel.open_wheel(path, isolated / "extracted")
        report = scan.scan_wheel(contents)

        assert report.verdict == "review", (
            f"the platform's own scanner would not let an operator install this: "
            f"{report.verdict} — {list(report.reasons)}"
        )
        reasons = " ".join(str(reason) for reason in report.reasons).lower()
        assert "httpx" in reasons, "the network client is what an operator should be told about"

    def test_the_dry_run_install_accepts_it_and_writes_nothing(self, isolated, capsys, monkeypatch):
        """The whole pipeline an operator runs, against this distribution: hash,
        scan, declaration check, and the pip command it WOULD run."""
        from robothor.cli.plugins import cmd_plugin
        from robothor.plugins import installer

        pip_calls: list[list[str]] = []

        def _pip(command, **_kw):
            pip_calls.append(list(command))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(installer, "_run_pip", _pip)
        path, data = _build_wheel(isolated)

        code = cmd_plugin(
            argparse.Namespace(
                plugin_command="install",
                name=str(path),
                version=None,
                index=None,
                accept_review=True,
                dry_run=True,
                sha256=hashlib.sha256(data).hexdigest(),
                scan_prompts=False,
                force=False,
            )
        )
        out = capsys.readouterr().out
        assert code == 0, out
        assert "review" in out
        assert "would run" in out
        assert pip_calls == [], "a dry run installed something"
        assert not (isolated / "plugins.lock").exists()

    def test_a_wrong_hash_is_refused(self, isolated, capsys, monkeypatch):
        from robothor.cli.plugins import cmd_plugin
        from robothor.plugins import installer

        monkeypatch.setattr(installer, "_run_pip", lambda *_a, **_kw: None)
        path, _data = _build_wheel(isolated)
        code = cmd_plugin(
            argparse.Namespace(
                plugin_command="install",
                name=str(path),
                version=None,
                index=None,
                accept_review=True,
                dry_run=True,
                sha256="0" * 64,
                scan_prompts=False,
                force=False,
            )
        )
        assert code == 2
        assert "Traceback" not in capsys.readouterr().err
