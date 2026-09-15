"""Installing an agent bundle somebody else exported.

Everything a bundle carries arrived from outside this instance, so every
property below is a refusal: a hash that does not match, a member nothing
vouched for, an id that would overwrite a live agent, a wheel wearing a
bundle's name, and the hostile tarballs the hub client already knows about.

Nothing reaches the workspace until ``yes=True``, and the plan an operator is
shown is built from the same verified bundle the install then writes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile

import httpx
import pytest
import yaml

from robothor.templates.bundle import BUNDLE_FILENAME, read_bundle
from robothor.templates.bundle_installer import (
    BundleInstallError,
    install_bundle,
    plan_install,
)
from robothor.templates.exporter import export_agent

MANIFEST = """\
id: test-agent
name: Test Agent
description: A test agent
version: "1.0.0"
department: custom

model:
  primary: openrouter/xiaomi/mimo-v2-pro

schedule:
  cron: "0 * * * *"
  timezone: UTC

delivery:
  mode: none

tools_allowed: []
instruction_file: brain/agents/test-agent.md
"""

SCHEMA = {
    "required": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "string"},
        "department": {"type": "string", "enum": ["custom"]},
    }
}


def _workspace(root):
    """A workspace with the directories the installer writes into."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "brain" / "agents").mkdir(parents=True)
    (root / "templates" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "schema.yaml").write_text(yaml.dump(SCHEMA))
    (root / "templates" / "agents" / "_defaults.yaml").write_text(
        yaml.dump({"model_primary": "openrouter/xiaomi/mimo-v2-pro", "timezone": "UTC"})
    )
    return root


def _install_agent(repo, *, manifest: str = MANIFEST, instructions: str = "# Test Agent\n"):
    (repo / "docs" / "agents" / "test-agent.yaml").write_text(manifest)
    (repo / "brain" / "agents" / "test-agent.md").write_text(instructions)


@pytest.fixture
def source_repo(tmp_path):
    repo = _workspace(tmp_path / "source")
    _install_agent(repo)
    return repo


@pytest.fixture
def target(tmp_path):
    repo = _workspace(tmp_path / "target")
    instance_dir = tmp_path / "target-instance"
    instance_dir.mkdir()
    return repo, instance_dir


@pytest.fixture
def bundle_dir(source_repo, tmp_path):
    out = tmp_path / "bundle"
    export_agent("test-agent", out=out, repo_root=source_repo)
    return out


@pytest.fixture
def bundle_archive(source_repo, tmp_path):
    out = tmp_path / "test-agent.tar.gz"
    export_agent("test-agent", out=out, repo_root=source_repo)
    return out


def _no_secrets(_name: str) -> str:
    return "missing"


def _kwargs(target, **extra):
    repo, instance_dir = target
    base = {
        "repo_root": repo,
        "instance_dir": instance_dir,
        "environment": {},
        "secret_lookup": _no_secrets,
        "adapter_dir": None,
    }
    base.update(extra)
    return base


class TestPlan:
    def test_plan_names_what_would_be_written_and_writes_nothing(self, bundle_dir, target):
        repo, _ = target
        plan = plan_install(bundle_dir, **_kwargs(target))

        assert plan.target_id == "test-agent"
        assert "docs/agents/test-agent.yaml" in plan.writes
        assert "brain/agents/test-agent.md" in plan.writes
        assert not plan.collision
        assert not (repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_plan_lists_every_requirement_with_its_status(self, source_repo, tmp_path, target):
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  plugins: [genus-billing]\n  secrets: [BILLING_API_KEY]\n"
        )
        out = tmp_path / "with-requires"
        export_agent("test-agent", out=out, repo_root=source_repo)

        plan = plan_install(out, **_kwargs(target))

        statuses = {(s.kind, s.name): s.satisfied for s in plan.requires}
        assert statuses[("plugins", "genus-billing")] is False
        assert statuses[("secrets", "BILLING_API_KEY")] is False
        assert [s.name for s in plan.unsatisfied()]

    def test_a_secret_present_in_the_environment_is_satisfied(self, source_repo, tmp_path, target):
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  secrets: [BILLING_API_KEY]\n"
        )
        out = tmp_path / "with-secret"
        export_agent("test-agent", out=out, repo_root=source_repo)

        plan = plan_install(out, **_kwargs(target, environment={"BILLING_API_KEY": "x"}))

        assert all(s.satisfied for s in plan.requires if s.kind == "secrets")


class TestInstall:
    def test_yes_writes_the_agent_through_the_installer(self, bundle_dir, target):
        repo, instance_dir = target
        plan, result = install_bundle(bundle_dir, yes=True, **_kwargs(target))

        assert result is not None
        manifest = yaml.safe_load((repo / "docs" / "agents" / "test-agent.yaml").read_text())
        assert manifest["id"] == "test-agent"
        assert (repo / "brain" / "agents" / "test-agent.md").exists()

        from robothor.templates.instance import InstanceConfig

        record = InstanceConfig.load(instance_dir).installed_agents["test-agent"]
        assert record["source"] == "bundle"

    def test_an_archive_installs_the_same_way(self, bundle_archive, target):
        repo, _ = target
        _, result = install_bundle(bundle_archive, yes=True, **_kwargs(target))
        assert result is not None
        assert (repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_an_archive_with_the_wrong_sha256_is_refused(self, bundle_archive, target):
        with pytest.raises(BundleInstallError, match="SHA-256|checksum|hash"):
            install_bundle(bundle_archive, yes=True, sha256="b" * 64, **_kwargs(target))

    def test_an_archive_with_the_right_sha256_installs(self, bundle_archive, target):
        digest = hashlib.sha256(bundle_archive.read_bytes()).hexdigest()
        _, result = install_bundle(bundle_archive, yes=True, sha256=digest, **_kwargs(target))
        assert result is not None

    def test_carried_skills_are_written_once(self, source_repo, tmp_path, target):
        repo, _ = target
        skill = source_repo / "agents" / "skills" / "triage"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Triage\n")
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  skills: [triage]\n"
        )
        out = tmp_path / "with-skill"
        export_agent("test-agent", out=out, repo_root=source_repo)

        plan, _ = install_bundle(out, yes=True, **_kwargs(target))

        assert plan.skills == ("triage",)
        assert (repo / "agents" / "skills" / "triage" / "SKILL.md").read_text() == "# Triage\n"

    def test_an_existing_skill_is_kept_not_overwritten(self, source_repo, tmp_path, target):
        repo, _ = target
        skill = source_repo / "agents" / "skills" / "triage"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Theirs\n")
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  skills: [triage]\n"
        )
        out = tmp_path / "with-skill"
        export_agent("test-agent", out=out, repo_root=source_repo)
        mine = repo / "agents" / "skills" / "triage"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("# Mine\n")

        plan, _ = install_bundle(out, yes=True, **_kwargs(target))

        assert plan.kept_skills == ("triage",)
        assert (mine / "SKILL.md").read_text() == "# Mine\n"


class TestCollision:
    def test_an_installed_id_is_refused(self, bundle_dir, target):
        repo, _ = target
        _install_agent(repo)
        with pytest.raises(BundleInstallError, match="already"):
            install_bundle(bundle_dir, yes=True, **_kwargs(target))

    def test_the_plan_reports_the_collision_before_any_write(self, bundle_dir, target):
        repo, _ = target
        _install_agent(repo)
        plan = plan_install(bundle_dir, **_kwargs(target))
        assert plan.collision

    def test_new_id_renames_the_manifest_the_instruction_and_the_file(self, bundle_dir, target):
        repo, _ = target
        _install_agent(repo)

        install_bundle(bundle_dir, yes=True, new_id="triage-bot", **_kwargs(target))

        manifest = yaml.safe_load((repo / "docs" / "agents" / "triage-bot.yaml").read_text())
        assert manifest["id"] == "triage-bot"
        assert manifest["instruction_file"] == "brain/agents/triage-bot.md"
        assert (repo / "brain" / "agents" / "triage-bot.md").exists()
        assert (repo / "docs" / "agents" / "test-agent.yaml").read_text() == MANIFEST


class TestIntegrity:
    def test_a_tampered_member_is_refused_before_any_write(self, bundle_dir, target):
        repo, _ = target
        (bundle_dir / "instructions.template.md").write_text("# Replaced\n")
        with pytest.raises(BundleInstallError, match="instructions.template.md"):
            install_bundle(bundle_dir, yes=True, **_kwargs(target))
        assert not (repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_a_member_the_file_list_omits_is_refused(self, bundle_dir, target):
        document = yaml.safe_load((bundle_dir / BUNDLE_FILENAME).read_text())
        document["files"] = [f for f in document["files"] if f["path"] != "SKILL.md"]
        (bundle_dir / BUNDLE_FILENAME).write_text(yaml.dump(document, sort_keys=False))

        with pytest.raises(BundleInstallError, match="SKILL.md"):
            install_bundle(bundle_dir, yes=True, **_kwargs(target))

    def test_a_directory_with_no_bundle_yaml_is_refused(self, bundle_dir, target):
        (bundle_dir / BUNDLE_FILENAME).unlink()
        with pytest.raises(BundleInstallError, match=BUNDLE_FILENAME):
            plan_install(bundle_dir, **_kwargs(target))


class TestStrict:
    def test_strict_refuses_an_unsatisfied_requirement(self, source_repo, tmp_path, target):
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  plugins: [genus-billing]\n"
        )
        out = tmp_path / "strict"
        export_agent("test-agent", out=out, repo_root=source_repo)

        with pytest.raises(BundleInstallError, match="genus-billing"):
            install_bundle(out, yes=True, strict=True, **_kwargs(target))

    def test_without_strict_an_unsatisfied_requirement_only_warns(
        self, source_repo, tmp_path, target
    ):
        (source_repo / "docs" / "agents" / "test-agent.yaml").write_text(
            MANIFEST + "\nrequires:\n  plugins: [genus-billing]\n"
        )
        out = tmp_path / "loose"
        export_agent("test-agent", out=out, repo_root=source_repo)

        _, result = install_bundle(out, yes=True, **_kwargs(target))
        assert result is not None


class TestHostileArchives:
    @staticmethod
    def _archive(members: list[tarfile.TarInfo], payloads: list[bytes]) -> bytes:
        raw = io.BytesIO()
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for info, payload in zip(members, payloads, strict=True):
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
        return raw.getvalue()

    def _write(self, tmp_path, data: bytes):
        path = tmp_path / "hostile.tar.gz"
        path.write_bytes(data)
        return path

    def test_a_traversing_member_is_refused(self, tmp_path, target):
        info = tarfile.TarInfo("../escape.yaml")
        data = self._archive([info], [b"owned\n"])
        with pytest.raises(BundleInstallError):
            plan_install(self._write(tmp_path, data), **_kwargs(target))

    def test_a_symlink_member_is_refused(self, tmp_path, target):
        info = tarfile.TarInfo("bundle/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        data = self._archive([info], [b""])
        with pytest.raises(BundleInstallError):
            plan_install(self._write(tmp_path, data), **_kwargs(target))

    def test_an_archive_with_too_many_members_is_refused(self, tmp_path, target):
        members = [tarfile.TarInfo(f"bundle/f{i}") for i in range(20000)]
        data = self._archive(members, [b"x"] * len(members))
        with pytest.raises(BundleInstallError):
            plan_install(self._write(tmp_path, data), **_kwargs(target))

    def test_an_archive_that_is_not_a_bundle_is_refused(self, tmp_path, target):
        info = tarfile.TarInfo("bundle/setup.yaml")
        data = self._archive([info], [b"agent_id: x\n"])
        with pytest.raises(BundleInstallError, match=BUNDLE_FILENAME):
            plan_install(self._write(tmp_path, data), **_kwargs(target))


class TestWrongThing:
    def test_a_plugin_wheel_is_refused_with_the_verb_that_takes_it(self, tmp_path, target):
        wheel = tmp_path / "genus_billing-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04not-a-bundle")
        with pytest.raises(BundleInstallError, match="genus plugin install"):
            plan_install(wheel, **_kwargs(target))

    def test_a_plain_file_is_refused(self, tmp_path, target):
        path = tmp_path / "notes.txt"
        path.write_text("hello\n")
        with pytest.raises(BundleInstallError):
            plan_install(path, **_kwargs(target))


class TestUrl:
    @staticmethod
    def _client(body: bytes):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_a_url_without_a_sha256_is_refused_before_the_fetch(self, target):
        with pytest.raises(BundleInstallError, match="--sha256"):
            plan_install("https://example.invalid/agent.tar.gz", **_kwargs(target))

    def test_an_http_url_is_refused(self, target):
        with pytest.raises(BundleInstallError, match="https"):
            plan_install("http://example.invalid/agent.tar.gz", sha256="a" * 64, **_kwargs(target))

    def test_a_url_whose_bytes_match_the_sha256_installs(self, bundle_archive, target):
        repo, _ = target
        body = bundle_archive.read_bytes()
        digest = hashlib.sha256(body).hexdigest()

        _, result = install_bundle(
            "https://example.invalid/agent.tar.gz",
            yes=True,
            sha256=digest,
            client=self._client(body),
            **_kwargs(target),
        )

        assert result is not None
        assert (repo / "docs" / "agents" / "test-agent.yaml").exists()

    def test_a_url_whose_bytes_do_not_match_is_refused(self, bundle_archive, target):
        body = bundle_archive.read_bytes()
        with pytest.raises(BundleInstallError, match="SHA-256|checksum|hash"):
            plan_install(
                "https://example.invalid/agent.tar.gz",
                sha256="c" * 64,
                client=self._client(body),
                **_kwargs(target),
            )


class TestSourceIsLeftAlone:
    def test_installing_from_a_directory_does_not_modify_it(self, bundle_dir, target):
        before = read_bundle(bundle_dir)
        install_bundle(bundle_dir, yes=True, new_id="triage-bot", **_kwargs(target))
        assert read_bundle(bundle_dir) == before
        assert "id: test-agent" in (bundle_dir / "manifest.template.yaml").read_text()
