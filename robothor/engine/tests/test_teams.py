"""Tests for agent teams and scratchpad."""

from __future__ import annotations

from unittest.mock import MagicMock

from robothor.engine.teams import Team, TeamManager, init_team_manager


class TestTeam:
    def test_default_timestamp(self):
        t = Team(team_id="t1", member_ids=["a", "b"])
        assert t.created_at > 0

    def test_fields(self):
        t = Team(team_id="research", member_ids=["main", "analyst"], objective="find data")
        assert t.team_id == "research"
        assert t.objective == "find data"
        assert len(t.member_ids) == 2


class TestTeamManager:
    def _mock_redis(self):
        """Create a mock Redis with in-memory hash storage."""
        r = MagicMock()
        store: dict[str, dict[str, str]] = {}

        def hset(key, *args, mapping=None, **kwargs):
            if mapping:
                store.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})
            elif len(args) >= 2:
                store.setdefault(key, {})[str(args[0])] = str(args[1])

        def hgetall(key):
            return {k.encode(): v.encode() for k, v in store.get(key, {}).items()}

        def hget(key, field):
            val = store.get(key, {}).get(field)
            return val.encode() if val else None

        def keys(pattern):
            import fnmatch

            return [k.encode() for k in store if fnmatch.fnmatch(k, pattern)]

        def delete(*del_keys):
            for k in del_keys:
                store.pop(k, None)

        r.hset = MagicMock(side_effect=hset)
        r.hgetall = MagicMock(side_effect=hgetall)
        r.hget = MagicMock(side_effect=hget)
        r.keys = MagicMock(side_effect=keys)
        r.delete = MagicMock(side_effect=delete)
        r._store = store
        return r

    def test_create_team(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        team = tm.create_team("alpha", ["a", "b"], "research")
        assert team is not None
        assert team.team_id == "alpha"
        assert team.member_ids == ["a", "b"]

    def test_get_team(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("beta", ["x", "y"], "review")
        team = tm.get_team("beta")
        assert team is not None
        assert team.objective == "review"
        assert "x" in team.member_ids

    def test_get_nonexistent_team(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        assert tm.get_team("nope") is None

    def test_list_teams(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("t1", ["a"])
        tm.create_team("t2", ["b"])
        teams = tm.list_teams()
        assert len(teams) == 2
        ids = {t.team_id for t in teams}
        assert ids == {"t1", "t2"}

    def test_dissolve_team(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("doomed", ["a"])
        assert tm.dissolve_team("doomed") is True
        assert tm.get_team("doomed") is None

    def test_get_agent_teams(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("t1", ["agent-a", "agent-b"])
        tm.create_team("t2", ["agent-b", "agent-c"])
        assert tm.get_agent_teams("agent-b") == ["t1", "t2"]
        assert tm.get_agent_teams("agent-a") == ["t1"]
        assert tm.get_agent_teams("agent-z") == []

    def test_scratchpad_write_and_read(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("sp", ["a"])
        assert tm.scratchpad_write("sp", "status", "in_progress", agent_id="a") is True
        data = tm.scratchpad_read("sp", "status")
        assert "status" in data
        assert data["status"]["value"] == "in_progress"
        assert data["status"]["updated_by"] == "a"

    def test_scratchpad_read_all(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        tm.create_team("sp2", ["a"])
        tm.scratchpad_write("sp2", "k1", "v1", agent_id="a")
        tm.scratchpad_write("sp2", "k2", "v2", agent_id="a")
        data = tm.scratchpad_read("sp2")
        assert "k1" in data
        assert "k2" in data

    def test_scratchpad_read_empty(self):
        r = self._mock_redis()
        tm = TeamManager(redis_client=r)
        data = tm.scratchpad_read("nonexistent")
        assert data == {}

    def test_no_redis(self):
        tm = TeamManager(redis_client=None)
        tm._get_redis = lambda: None  # type: ignore[method-assign]
        assert tm.create_team("x", ["a"]) is None
        assert tm.get_team("x") is None
        assert tm.list_teams() == []
        assert tm.dissolve_team("x") is False
        assert tm.scratchpad_write("x", "k", "v") is False
        assert tm.scratchpad_read("x") == {}


class TestTeamManagerSingleton:
    def test_init_and_get(self):
        from robothor.engine.teams import get_team_manager

        tm = init_team_manager(redis_client=MagicMock())
        assert get_team_manager() is tm
