"""The devops report's team roster — a YAML file the instance owns.

The weekly report counts people on this roster and nobody else. The roster
used to be derived from CRM contact identifiers, which counted ex-staff,
missed current engineers and produced duplicate rows; a hand-kept file the
operator edits is the one source of who is on the team and how each role is
judged.

Location: ``$DEVOPS_TEAM_FILE``, default
``$ROBOTHOR_WORKSPACE/local/devops/team.yaml``. Schema::

    roles:
      senior: {title: Senior Engineer, focus: "how this role is judged"}
    members:
      - id: alice
        name: Alice Example
        role: senior              # key into roles
        github: [alice-gh]
        jira: ["Alice Example"]   # JIRA display name(s)
        usage_name: Alice         # name used in the HR Claude usage report
        active: true              # false = ex-staff, never a row
    bots_excluded: [dependabot[bot], github-actions[bot]]

A handle on the roster is counted even if it looks like a bot (an AI review
agent can be a member). Any other handle ending in ``[bot]``, or listed in
``bots_excluded``, is a bot and only ever appears on the report's bots line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHANNELS = ("github", "jira")


class TeamFileError(ValueError):
    """The team file is missing or invalid."""


@dataclass(frozen=True)
class Role:
    key: str
    title: str
    focus: str = ""


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    role: str
    github: tuple[str, ...] = ()
    jira: tuple[str, ...] = ()
    usage_name: str | None = None
    active: bool = True


@dataclass
class Team:
    roles: dict[str, Role]
    members: list[Member]
    bots_excluded: frozenset[str] = frozenset()
    _index: dict[tuple[str, str], Member] = field(default_factory=dict, repr=False)

    def member_for(self, channel: str, handle: str) -> Member | None:
        """The roster member (active or not) owning ``handle`` on ``channel``."""
        return self._index.get((channel, _norm(handle)))

    def is_bot(self, handle: str) -> bool:
        """A bot that is not a roster member."""
        if self.member_for("github", handle) is not None:
            return False
        h = _norm(handle)
        return h in self.bots_excluded or h.endswith("[bot]")

    @property
    def active_members(self) -> list[Member]:
        return [m for m in self.members if m.active]

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)


def _norm(handle: str) -> str:
    return " ".join(str(handle).split()).casefold()


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TeamFileError(f"{where} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def parse_team(raw: Any) -> Team:
    """Validate a parsed team file. Raises :class:`TeamFileError`."""
    if not isinstance(raw, dict):
        raise TeamFileError("team file must be a mapping")
    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, dict):
        raise TeamFileError("roles must be a mapping of role key -> {title, focus}")
    roles: dict[str, Role] = {}
    for key, spec in roles_raw.items():
        spec = spec or {}
        if isinstance(spec, str):
            spec = {"title": spec}
        roles[str(key)] = Role(
            key=str(key),
            title=str(spec.get("title") or key),
            focus=str(spec.get("focus") or ""),
        )

    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise TeamFileError("members must be a non-empty list")

    members: list[Member] = []
    index: dict[tuple[str, str], Member] = {}
    ids: set[str] = set()
    for i, m in enumerate(members_raw):
        if not isinstance(m, dict):
            raise TeamFileError(f"members[{i}] must be a mapping")
        mid = str(m.get("id") or "").strip()
        if not mid:
            raise TeamFileError(f"members[{i}] has no id")
        if mid in ids:
            raise TeamFileError(f"duplicate member id {mid!r}")
        ids.add(mid)
        role = str(m.get("role") or "").strip()
        if role not in roles:
            raise TeamFileError(f"member {mid!r} has unknown role {role!r}")
        usage = m.get("usage_name")
        member = Member(
            id=mid,
            name=str(m.get("name") or mid),
            role=role,
            github=_str_list(m.get("github"), f"member {mid!r} github"),
            jira=_str_list(m.get("jira"), f"member {mid!r} jira"),
            usage_name=str(usage).strip() if usage else None,
            active=bool(m.get("active", True)),
        )
        for channel in CHANNELS:
            for handle in getattr(member, channel):
                key = (channel, _norm(handle))
                if key in index:
                    raise TeamFileError(
                        f"{channel} handle {handle!r} is listed for both "
                        f"{index[key].id!r} and {mid!r}"
                    )
                index[key] = member
        members.append(member)

    bots = frozenset(_norm(b) for b in _str_list(raw.get("bots_excluded"), "bots_excluded"))
    return Team(roles=roles, members=members, bots_excluded=bots, _index=index)


def devops_env(name: str, default: str = "") -> str:
    """The one reader of the report's environment settings (``DEVOPS_*``, workspace).

    The report's scripts run standalone, outside the engine's settings object;
    funnelling every read through here keeps them to a single call site.
    """
    return os.environ.get(name, default).strip()


def env_list(name: str) -> list[str]:
    """A comma-separated environment setting as a list."""
    return [v.strip() for v in devops_env(name).split(",") if v.strip()]


def workspace() -> Path:
    env = devops_env("ROBOTHOR_WORKSPACE")
    return Path(env) if env else Path.home() / "robothor"


def team_file_path() -> Path:
    env = devops_env("DEVOPS_TEAM_FILE")
    return Path(env) if env else workspace() / "local" / "devops" / "team.yaml"


def runs_dir() -> Path:
    env = devops_env("DEVOPS_RUNS_DIR")
    return Path(env) if env else workspace() / "local" / "devops" / "runs"


def week_run_dir(week_start: str, base: Path | None = None) -> Path:
    """``<runs dir>/<week-start>/`` — every intermediate file of one report week."""
    d = (base or runs_dir()) / week_start
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_team(path: Path | None = None) -> Team:
    import yaml

    p = path or team_file_path()
    if not p.exists():
        raise TeamFileError(f"team file not found: {p} (set DEVOPS_TEAM_FILE)")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise TeamFileError(f"team file {p} is not valid YAML: {e}") from e
    return parse_team(raw)
