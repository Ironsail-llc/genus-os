"""Agent teams — named groups with shared scratchpad.

Teams provide a coordination layer for agents that need to collaborate
on a shared objective. Each team has:
- A name and objective
- A list of member agent IDs
- A shared scratchpad (key-value store)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TEAM_PREFIX = "robothor:team:"
SCRATCHPAD_SUFFIX = ":scratchpad"


@dataclass
class Team:
    """A named group of agents."""

    team_id: str
    member_ids: list[str]
    objective: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


class TeamManager:
    """Manages named agent groups with shared scratchpad."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    def _get_redis(self) -> Any:
        if self._redis is not None:
            return self._redis
        try:
            import redis

            from robothor.config import get_config

            cfg = get_config()
            self._redis = redis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=cfg.redis.password or None,
            )
            return self._redis
        except Exception as e:
            logger.error("Failed to connect to Redis for teams: %s", e)
            return None

    def create_team(
        self,
        team_id: str,
        member_ids: list[str],
        objective: str = "",
    ) -> Team | None:
        """Create a new team."""
        r = self._get_redis()
        if r is None:
            return None

        team = Team(team_id=team_id, member_ids=member_ids, objective=objective)
        key = f"{TEAM_PREFIX}{team_id}"

        try:
            r.hset(
                key,
                mapping={
                    "team_id": team_id,
                    "member_ids": json.dumps(member_ids),
                    "objective": objective,
                    "created_at": str(team.created_at),
                },
            )
            return team
        except Exception as e:
            logger.error("Failed to create team %s: %s", team_id, e)
            return None

    def get_team(self, team_id: str) -> Team | None:
        """Get a team by ID."""
        r = self._get_redis()
        if r is None:
            return None

        key = f"{TEAM_PREFIX}{team_id}"
        try:
            data = r.hgetall(key)
            if not data:
                return None
            # Decode bytes
            decoded = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in data.items()
            }
            return Team(
                team_id=decoded.get("team_id", team_id),
                member_ids=json.loads(decoded.get("member_ids", "[]")),
                objective=decoded.get("objective", ""),
                created_at=float(decoded.get("created_at", 0)),
            )
        except Exception as e:
            logger.error("Failed to get team %s: %s", team_id, e)
            return None

    def list_teams(self) -> list[Team]:
        """List all teams."""
        r = self._get_redis()
        if r is None:
            return []

        try:
            keys = r.keys(f"{TEAM_PREFIX}*")
            teams = []
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                # Skip scratchpad keys
                if key_str.endswith(SCRATCHPAD_SUFFIX):
                    continue
                team_id = key_str.removeprefix(TEAM_PREFIX)
                team = self.get_team(team_id)
                if team:
                    teams.append(team)
            return teams
        except Exception as e:
            logger.error("Failed to list teams: %s", e)
            return []

    def dissolve_team(self, team_id: str) -> bool:
        """Remove a team and its scratchpad."""
        r = self._get_redis()
        if r is None:
            return False

        try:
            r.delete(f"{TEAM_PREFIX}{team_id}")
            r.delete(f"{TEAM_PREFIX}{team_id}{SCRATCHPAD_SUFFIX}")
            return True
        except Exception as e:
            logger.error("Failed to dissolve team %s: %s", team_id, e)
            return False

    def get_agent_teams(self, agent_id: str) -> list[str]:
        """Get all team IDs an agent belongs to."""
        teams = self.list_teams()
        return [t.team_id for t in teams if agent_id in t.member_ids]

    # ── Scratchpad ───────────────────────────────────────────────────

    def scratchpad_write(self, team_id: str, key: str, value: str, agent_id: str = "") -> bool:
        """Write to a team's shared scratchpad."""
        r = self._get_redis()
        if r is None:
            return False

        scratchpad_key = f"{TEAM_PREFIX}{team_id}{SCRATCHPAD_SUFFIX}"
        try:
            entry = json.dumps(
                {
                    "value": value,
                    "updated_by": agent_id,
                    "updated_at": time.time(),
                }
            )
            r.hset(scratchpad_key, key, entry)
            return True
        except Exception as e:
            logger.error("Failed to write scratchpad %s/%s: %s", team_id, key, e)
            return False

    def scratchpad_read(self, team_id: str, key: str | None = None) -> dict[str, Any]:
        """Read from a team's shared scratchpad.

        If key is None, returns all entries. Otherwise returns a single entry.
        """
        r = self._get_redis()
        if r is None:
            return {}

        scratchpad_key = f"{TEAM_PREFIX}{team_id}{SCRATCHPAD_SUFFIX}"
        try:
            if key is not None:
                raw = r.hget(scratchpad_key, key)
                if raw is None:
                    return {}
                data = raw.decode() if isinstance(raw, bytes) else raw
                return {key: json.loads(data)}
            else:
                raw_all = r.hgetall(scratchpad_key)
                result = {}
                for k, v in raw_all.items():
                    k_str = k.decode() if isinstance(k, bytes) else k
                    v_str = v.decode() if isinstance(v, bytes) else v
                    result[k_str] = json.loads(v_str)
                return result
        except Exception as e:
            logger.error("Failed to read scratchpad %s: %s", team_id, e)
            return {}


# Module-level singleton
_team_manager: TeamManager | None = None


def get_team_manager() -> TeamManager | None:
    return _team_manager


def init_team_manager(redis_client: Any = None) -> TeamManager:
    global _team_manager
    _team_manager = TeamManager(redis_client)
    return _team_manager
