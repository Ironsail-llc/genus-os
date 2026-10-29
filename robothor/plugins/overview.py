"""What each installed plugin wrote into the workspace, read without writing.

Three readers ask the same question -- the ``plugins_overview`` tool, the admin
plugins payload the Helm's Plugins page draws, and the bridge's workflow
listing -- so they share this module rather than each walking the ledger.

The facts come from two records the installer already keeps:

* ``.robothor/plugin-assets.yaml`` (:mod:`robothor.plugins.assets`): per
  distribution, the agents it rendered, the files it copied (a workflow is a
  file under ``docs/workflows/``) and the manifests it patched;
* ``.robothor/installed.yaml``: every agent recorded with
  ``source: plugin:<dist>``, which also covers an agent the plugin adopted.

Everything here is read-only. In particular it never calls
:func:`robothor.plugins.assets.read_ledger` on an instance directory that does
not exist, because resolving the ledger path creates the directory. A damaged
ledger is a warning, never an exception: a listing that 500s because one file is
unreadable hides every plugin that is fine. No value returned carries an
absolute path; files are workspace-relative, as the ledger records them.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from robothor.plugins.assets import LEDGER_NAME, AssetError, canonical, read_ledger
from robothor.templates.safety import (
    TemplateSecurityError,
    default_workspace_root,
    validate_identifier,
    workspace_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WORKFLOW_PREFIX",
    "empty_assets",
    "parse_workflow_file",
    "trigger_summary",
    "workflow_sources",
    "workspace_assets",
]

#: Where a plugin's workflows land. A ledger ``files`` entry under this prefix
#: is reported as a workflow, every other one as a file.
WORKFLOW_PREFIX = "docs/workflows/"

_WORKFLOW_SUFFIXES = (".yaml", ".yml")


def empty_assets() -> dict[str, list[Any]]:
    """The shape every plugin carries, for one that wrote nothing."""
    return {"agents": [], "workflows": [], "patches": [], "files": []}


def trigger_summary(trigger: Any) -> str:
    """One trigger as a short phrase: ``cron 0 7 * * *``, ``hook email:email.new``."""
    kind = str(getattr(trigger, "type", "") or "")
    if kind == "cron":
        cron = str(getattr(trigger, "cron", "") or "")
        return f"cron {cron}".strip()
    if kind == "hook":
        where = ":".join(
            part
            for part in (
                str(getattr(trigger, "stream", "") or ""),
                str(getattr(trigger, "event_type", "") or ""),
            )
            if part
        )
        return f"hook {where}".strip()
    return kind or "unknown"


def _resolve_workspace(workspace: Path | None) -> Path | None:
    try:
        root = workspace if workspace is not None else default_workspace_root()
    except Exception:  # noqa: BLE001 - no workspace means nothing was written
        return None
    # Resolved once, here: a platform temp root may be reached through a
    # symlink (macOS /var -> /private/var). Child paths are still checked for
    # escaping symlinks by ``workspace_path``.
    resolved = Path(root).expanduser().resolve()
    return resolved if resolved.is_dir() else None


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _ledger(root: Path, warnings: list[str]) -> dict[str, Any]:
    instance = root / ".robothor"
    if not (instance / LEDGER_NAME).is_file():
        return {}
    try:
        return read_ledger(instance)
    except (AssetError, TemplateSecurityError, OSError) as exc:
        warnings.append(str(exc))
        return {}


def _installed_plugin_agents(root: Path, warnings: list[str]) -> dict[str, set[str]]:
    """Canonical distribution -> agent ids ``installed.yaml`` records as its."""
    path = root / ".robothor" / "installed.yaml"
    if not path.is_file():
        return {}
    data = _read_yaml(path)
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        warnings.append("The installed agent registry (.robothor/installed.yaml) is not readable.")
        return {}
    owned: dict[str, set[str]] = {}
    for agent_id, record in agents.items():
        source = record.get("source") if isinstance(record, dict) else None
        if isinstance(source, str) and source.startswith("plugin:"):
            owned.setdefault(canonical(source.partition(":")[2]), set()).add(str(agent_id))
    return owned


def _agent(root: Path, agent_id: str) -> dict[str, Any]:
    """``{id, name, schedule}`` from the agent's manifest; the id alone if unreadable."""
    entry: dict[str, Any] = {"id": agent_id, "name": agent_id, "schedule": None}
    try:
        validate_identifier(agent_id, label="agent ID")
        path = workspace_path(
            root, f"docs/agents/{agent_id}.yaml", allowed_prefix="docs/agents", label="manifest"
        )
    except TemplateSecurityError:
        return entry
    data = _read_yaml(path) if path.is_file() else None
    if isinstance(data, dict):
        if isinstance(data.get("name"), str) and data["name"].strip():
            entry["name"] = data["name"].strip()
        schedule = data.get("schedule")
        cron = schedule.get("cron") if isinstance(schedule, dict) else None
        if isinstance(cron, str) and cron.strip():
            entry["schedule"] = cron.strip()
    return entry


def parse_workflow_file(path: Path) -> dict[str, Any] | None:
    """``{id, name, triggers}`` for one workflow file, or None when it does not parse."""
    from robothor.engine.workflow import parse_workflow

    data = _read_yaml(path)
    if not isinstance(data, dict) or "id" not in data:
        return None
    try:
        workflow = parse_workflow(data)
    except Exception:  # noqa: BLE001 - a malformed workflow is skipped, not fatal
        logger.debug("workflow %s does not parse", path.name, exc_info=True)
        return None
    return {
        "id": workflow.id,
        "name": workflow.name or workflow.id,
        "triggers": [trigger_summary(t) for t in workflow.triggers],
    }


def _workflow(root: Path, dest: str) -> dict[str, Any]:
    stem = PurePosixPath(dest).stem
    try:
        path = workspace_path(root, dest, allowed_prefix="docs/workflows", label="workflow")
    except TemplateSecurityError:
        path = None
    parsed = parse_workflow_file(path) if path is not None and path.is_file() else None
    return parsed or {"id": stem, "name": stem, "triggers": []}


def _is_workflow(dest: str) -> bool:
    return dest.startswith(WORKFLOW_PREFIX) and dest.endswith(_WORKFLOW_SUFFIXES)


def workspace_assets(
    workspace: Path | None = None, warnings: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Canonical distribution -> ``{version, agents, workflows, patches, files}``.

    ``agents`` are ``{id, name, schedule}``, ``workflows`` ``{id, name,
    triggers}``, ``patches`` the ids of agent manifests the plugin patched, and
    ``files`` every other workspace-relative destination it wrote. ``version``
    is what the ledger recorded, or None. Problems reading either record are
    appended to *warnings*.
    """
    notes: list[str] = warnings if warnings is not None else []
    root = _resolve_workspace(workspace)
    if root is None:
        return {}
    ledger = _ledger(root, notes)
    owned = _installed_plugin_agents(root, notes)

    out: dict[str, dict[str, Any]] = {}
    for dist in sorted({canonical(str(name)) for name in ledger} | set(owned)):
        entry = next(
            (v for k, v in ledger.items() if canonical(str(k)) == dist and isinstance(v, dict)),
            {},
        )
        agent_ids = {str(a) for a in entry.get("agents") or [] if isinstance(a, str)}
        agent_ids |= owned.get(dist, set())
        files = entry.get("files") or {}
        dests = sorted(str(d) for d in files) if isinstance(files, dict) else []
        patches = entry.get("patches") or []
        version = entry.get("version")
        out[dist] = {
            "version": str(version) if version else None,
            "agents": [_agent(root, a) for a in sorted(agent_ids)],
            "workflows": [_workflow(root, d) for d in dests if _is_workflow(d)],
            "patches": sorted(
                {str(p["agent"]) for p in patches if isinstance(p, dict) and p.get("agent")}
            ),
            "files": [d for d in dests if not _is_workflow(d)],
        }
    return out


def workflow_sources(workspace: Path | None = None) -> dict[str, str]:
    """Workflow file name -> ``plugin:<dist>`` for every workflow a plugin wrote."""
    root = _resolve_workspace(workspace)
    if root is None:
        return {}
    sources: dict[str, str] = {}
    for dist, entry in _ledger(root, []).items():
        files = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(files, dict):
            continue
        for dest in files:
            if _is_workflow(str(dest)):
                sources[PurePosixPath(str(dest)).name] = f"plugin:{canonical(str(dist))}"
    return sources
