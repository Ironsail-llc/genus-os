"""Build and verify complete fleet release artifacts without installing them.

This reuses native manifest/workflow contracts, plugin-wheel inspection and
bundle path/credential checks. A verified artifact is not runtime readiness or
authorization to activate schedules, purchase inference or send messages.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from robothor.templates.bundle import MAX_BUNDLE_FILES, scan_secret_literals
from robothor.templates.safety import contained_path, safe_relative_path, trusted_directory


class ReleaseError(ValueError):
    """An invalid or changed release; no destination was activated."""


class ReleaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: str = Field(min_length=1, max_length=100)
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    platform_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    agents: list[str] = Field(min_length=1, max_length=100)
    workflows: list[str] = Field(default_factory=list, max_length=100)
    knowledge: list[str] = Field(default_factory=list, max_length=100)
    plugins: list[str] = Field(default_factory=list, max_length=10)
    sales_settings: str | None = None

    def paths(self):
        return [
            *self.agents,
            *self.workflows,
            *self.knowledge,
            *self.plugins,
            *([self.sales_settings] if self.sales_settings else []),
        ]

    @model_validator(mode="after")
    def canonical_inventory(self):
        paths = self.paths()
        if len(paths) > MAX_BUNDLE_FILES or len(set(paths)) != len(paths):
            raise ValueError("Release member count or duplicate path invalid")
        for path in paths:
            relative = safe_relative_path(path)
            if str(relative) != path or path == "release.json":
                raise ValueError("Canonical release member path required")
        for paths, prefix, suffix in (
            (self.agents, "docs/agents/", ".yaml"),
            (self.workflows, "docs/workflows/", ".yaml"),
            (self.knowledge, "brain/", ".md"),
            (self.plugins, "", ".whl"),
        ):
            if any(not p.startswith(prefix) or not p.endswith(suffix) for p in paths):
                raise ValueError("Release member role/path mismatch")
        return self


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(root, path):
    member = contained_path(root, path)
    limit = 50 * 1024 * 1024 if path.endswith(".whl") else 2 * 1024 * 1024
    if not member.is_file() or member.stat().st_size > limit:
        raise ReleaseError(f"Missing or oversized release member: {path}")
    data = member.read_bytes()
    if len(data) > limit:
        raise ReleaseError(f"Oversized release member: {path}")
    return data


def _contracts(root, spec):
    from apscheduler.triggers.cron import CronTrigger

    from robothor.engine.manifest_schema import validate
    from robothor.engine.workflow import parse_workflow
    from robothor.plugins.manifest import parse_manifest
    from robothor.plugins.wheel import open_wheel

    listed = set(spec.paths())
    agents, workflows, plugins = {}, {}, []
    documents = {}
    for path in listed - set(spec.plugins):
        content = _read(root, path).decode("utf-8")
        if scan_secret_literals(content, path):
            raise ReleaseError(f"Credential literal in release member: {path}")
        if path.endswith(".yaml"):
            document = yaml.safe_load(content)
            if not isinstance(document, dict):
                raise ReleaseError(f"Release document must be an object: {path}")
            documents[path] = document
    for path in spec.agents:
        raw = documents[path]
        issues = validate(raw, strict=True)
        if any(i.severity == "error" or i.code == "schema_unreadable" for i in issues):
            raise ReleaseError(f"Invalid native agent manifest: {path}")
        agent_id = raw["id"]
        if agent_id in agents or Path(path).stem != agent_id:
            raise ReleaseError("Duplicate agent or mismatched manifest filename")
        if raw.get("extends"):
            raise ReleaseError(
                "Release agents require explicit manifests without external inheritance"
            )
        refs = [
            raw.get("instruction_file"),
            *raw.get("bootstrap_files", []),
            *raw.get("warmup", {}).get("context_files", []),
        ]
        if not refs[0] or any(ref and ref not in spec.knowledge for ref in refs):
            raise ReleaseError(f"Agent knowledge reference missing from release: {path}")
        agents[agent_id] = raw
    for path in spec.workflows:
        raw = documents[path]
        workflow = parse_workflow(raw)
        if not workflow.id or workflow.id in workflows or Path(path).stem != workflow.id:
            raise ReleaseError("Duplicate workflow or mismatched workflow filename")
        steps = list(workflow.steps)
        seen = set()
        while steps:
            step = steps.pop()
            if not step.id or step.id in seen:
                raise ReleaseError("Workflow steps require distinct IDs")
            seen.add(step.id)
            if step.type == "agent" and step.agent_id not in agents:
                raise ReleaseError("Workflow agent is not included in release")
            if step.type == "tool" and not step.tool_name:
                raise ReleaseError("Workflow tool name required")
            steps.extend(step.parallel_steps)
        for trigger in workflow.triggers:
            if trigger.type == "cron":
                CronTrigger.from_crontab(trigger.cron, timezone=trigger.timezone)
        workflows[workflow.id] = workflow
    for path in spec.plugins:
        with tempfile.TemporaryDirectory(prefix="genus-release-wheel-") as folder:
            wheel = open_wheel(contained_path(root, path), Path(folder))
            manifest = parse_manifest(wheel.manifest_text)
            if (
                manifest is None
                or len(wheel.manifest_candidates) != 1
                or manifest.contract_version != 1
            ):
                raise ReleaseError("Plugin wheel needs one supported native manifest")
            if any(
                set(wheel.entry_points.get(group, {})) != manifest.declares_entry_points(group)
                for group in set(wheel.entry_points) | set(manifest.entry_points)
                if group.startswith("genus.")
            ):
                raise ReleaseError("Plugin entry points differ from their manifest")
            if any(p["name"] == wheel.name for p in plugins):
                raise ReleaseError("Duplicate plugin distribution")
            plugins.append(
                {
                    "path": path,
                    "name": wheel.name,
                    "version": wheel.version,
                    "manifest_sha256": wheel.manifest_sha256,
                }
            )
    if spec.sales_settings:
        from robothor.sales.models import SalesSettings

        settings = SalesSettings.model_validate(documents[spec.sales_settings])
        if any(
            getattr(settings, key)
            for key in (
                "research_enabled",
                "enrichment_enabled",
                "promotion_enabled",
                "sending_enabled",
                "outcomes_enabled",
            )
        ):
            raise ReleaseError("Build sales releases with integration switches disabled")
        if not set(settings.agents.values()) <= set(agents):
            raise ReleaseError("Sales settings refer to an agent outside the release")
        if set(settings.workflow_bindings.values()) != set(workflows):
            raise ReleaseError("Sales workflow bindings must cover the release workflows exactly")
        for stage, workflow_id in settings.workflow_bindings.items():
            workflow = workflows[workflow_id]
            if (
                len(workflow.steps) != 1
                or workflow.steps[0].type != "tool"
                or workflow.steps[0].tool_name != "sales_process_queue"
                or workflow.steps[0].tool_args != {"stage": stage}
            ):
                raise ReleaseError("Sales workflow does not match its bound queue stage")
    return {"agents": sorted(agents), "workflows": sorted(workflows), "plugins": plugins}


def build_release(source: Path, destination: Path, specification: dict) -> dict:
    """Publish a new verified artifact directory atomically; never overwrite one."""
    try:
        spec = ReleaseSpec.model_validate(specification)
        source = trusted_directory(source, label="release source")
        destination = Path(destination).absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = trusted_directory(destination.parent, label="release output parent")
        destination = parent / destination.name
        if destination.exists() or destination.is_symlink():
            raise ReleaseError("Release output already exists")
        with tempfile.TemporaryDirectory(prefix=".fleet-release-", dir=parent) as temporary:
            staging = Path(temporary) / "artifact"
            staging.mkdir()
            files = {}
            for path in sorted(spec.paths()):
                data = _read(source, path)
                output = contained_path(staging, path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                files[path] = {"sha256": _sha(data), "bytes": len(data)}
            document = {
                **spec.model_dump(mode="json"),
                "activation": "not_installed",
                "files": files,
                "contracts": _contracts(staging, spec),
            }
            document["release_id"] = _sha(_canonical(document))
            (staging / "release.json").write_bytes(_canonical(document))
            verify_release(staging, expected_digest=document["release_id"])
            staging.rename(destination)
            return document
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError(f"Release build refused ({type(exc).__name__})") from None


def verify_release(root: Path, *, expected_digest: str) -> dict:
    """Compare every member to the externally pinned release fingerprint."""
    try:
        root = trusted_directory(root, label="release artifact")
        document = json.loads(_read(root, "release.json"))
        payload = {key: value for key, value in document.items() if key != "release_id"}
        if (
            document["release_id"] != expected_digest
            or _sha(_canonical(payload)) != expected_digest
        ):
            raise ReleaseError("Release metadata differs from the pinned digest")
        spec = ReleaseSpec.model_validate(
            {
                key: value
                for key, value in payload.items()
                if key not in {"files", "contracts", "activation"}
            }
        )
        if document["activation"] != "not_installed":
            raise ReleaseError("Invalid release artifact state")
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ReleaseError("Release contains a symlink or special member")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != {*spec.paths(), "release.json"} or set(document["files"]) != set(spec.paths()):
            raise ReleaseError("Release inventory differs from the complete artifact")
        for path, expected in document["files"].items():
            data = _read(root, path)
            if expected != {"sha256": _sha(data), "bytes": len(data)}:
                raise ReleaseError(f"Release member drift: {path}")
        if _contracts(root, spec) != document["contracts"]:
            raise ReleaseError("Release contract summary differs from its contents")
        return document
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError(f"Release verification refused ({type(exc).__name__})") from None
