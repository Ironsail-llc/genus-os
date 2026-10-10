"""`v2.requires_human_tags` — an opt-in floor on what may wait for a person.

Agents parked their own work on the operator by filing tasks with
``requiresHuman: true`` — "main is red, 47 test failures", "file the fix as a
PR" — work the autonomous system exists to do. An instance that wants only
money-moving or signature work to wait on a human lists those tags in the
agent's manifest; the engine's task tools then refuse ``requiresHuman: true``
on any task that carries none of them, and SAY so, so the model acts instead
of the flag being silently dropped.

Absent key = the platform default: no restriction.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from robothor.engine import config as engine_config
from robothor.engine.tools import dispatch as _dispatch  # noqa: F401 — registry import order
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import crm as crm_handlers
from robothor.engine.tools.handlers.crm import HANDLERS

CTX = ToolContext(agent_id="worker-agent", tenant_id="test-tenant")
ALLOWED = ("payment", "refund", "wire", "contract")


def _gate(tags: tuple[str, ...] | None):
    return patch.object(crm_handlers, "requires_human_tags", return_value=tags or ())


class TestManifestKey:
    def test_absent_key_parses_to_empty(self):
        cfg = engine_config.manifest_to_agent_config({"id": "worker-agent", "name": "W"})
        assert cfg.requires_human_tags == []

    def test_key_parses_normalized(self):
        cfg = engine_config.manifest_to_agent_config(
            {
                "id": "worker-agent",
                "name": "W",
                "v2": {"requires_human_tags": ["Payment", " refund ", "", 7]},
            }
        )
        assert cfg.requires_human_tags == ["payment", "refund", "7"]

    def test_key_is_known_to_the_schema(self):
        from robothor.engine import manifest_schema

        assert "requires_human_tags" in manifest_schema._KNOWN_V2_KEYS

    def test_seam_reads_the_agents_manifest(self, tmp_path, monkeypatch):
        (tmp_path / "worker-agent.yaml").write_text(
            "id: worker-agent\nname: Worker\nv2:\n  requires_human_tags: [payment, Wire]\n"
        )
        monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path))
        assert crm_handlers.requires_human_tags("worker-agent") == ("payment", "wire")
        assert crm_handlers.requires_human_tags("no-such-agent") == ()
        assert crm_handlers.requires_human_tags("") == ()


class TestCreateTask:
    async def test_key_absent_is_unrestricted(self):
        with _gate(None), patch("robothor.crm.dal.create_task", return_value="t-1") as dal:
            result = await HANDLERS["create_task"](
                {"title": "Fix the red build", "requiresHuman": True}, CTX
            )
        assert result == {"id": "t-1", "title": "Fix the red build"}
        assert dal.call_args.kwargs["requires_human"] is True

    async def test_disallowed_is_rejected_with_a_clear_error(self):
        with _gate(ALLOWED), patch("robothor.crm.dal.create_task") as dal:
            result = await HANDLERS["create_task"](
                {"title": "Fix the red build", "requiresHuman": True, "tags": ["ci"]}, CTX
            )
        dal.assert_not_called()
        assert "error" in result
        err = result["error"]
        assert "requires_human is reserved for" in err
        for tag in ALLOWED:
            assert tag in err
        assert "This is yours to do" in err

    async def test_no_tags_at_all_is_rejected(self):
        with _gate(ALLOWED), patch("robothor.crm.dal.create_task") as dal:
            result = await HANDLERS["create_task"](
                {"title": "Optimize an agent", "requiresHuman": True}, CTX
            )
        dal.assert_not_called()
        assert "error" in result

    async def test_allowed_tag_passes(self):
        with _gate(ALLOWED), patch("robothor.crm.dal.create_task", return_value="t-2") as dal:
            result = await HANDLERS["create_task"](
                {"title": "Approve vendor invoice", "requiresHuman": True, "tags": ["payment"]},
                CTX,
            )
        assert result["id"] == "t-2"
        assert dal.call_args.kwargs["requires_human"] is True

    async def test_tag_match_is_case_insensitive(self):
        with _gate(ALLOWED), patch("robothor.crm.dal.create_task", return_value="t-3"):
            result = await HANDLERS["create_task"](
                {"title": "Sign the MSA", "requiresHuman": True, "tags": ["ops", "CONTRACT"]},
                CTX,
            )
        assert result["id"] == "t-3"

    async def test_requires_human_false_is_never_gated(self):
        with _gate(ALLOWED), patch("robothor.crm.dal.create_task", return_value="t-4"):
            result = await HANDLERS["create_task"]({"title": "Routine work"}, CTX)
        assert result["id"] == "t-4"


class TestUpdateTask:
    async def test_flip_without_allowed_tag_is_rejected(self):
        tid = str(uuid.uuid4())
        existing = {"id": tid, "title": "Main is red", "tags": ["ci"]}
        with (
            _gate(ALLOWED),
            patch("robothor.crm.dal.get_task", return_value=existing),
            patch("robothor.crm.dal.update_task") as dal,
        ):
            result = await HANDLERS["update_task"]({"id": tid, "requiresHuman": True}, CTX)
        dal.assert_not_called()
        assert "requires_human is reserved for" in result["error"]

    async def test_flip_uses_the_existing_tasks_tags(self):
        tid = str(uuid.uuid4())
        existing = {"id": tid, "title": "Refund customer", "tags": ["Refund"]}
        with (
            _gate(ALLOWED),
            patch("robothor.crm.dal.get_task", return_value=existing),
            patch("robothor.crm.dal.update_task", return_value=True) as dal,
        ):
            result = await HANDLERS["update_task"]({"id": tid, "requiresHuman": True}, CTX)
        assert result == {"success": True, "id": tid}
        assert dal.call_args.kwargs["requires_human"] is True

    async def test_flip_with_new_allowed_tags_in_the_same_call_passes(self):
        tid = str(uuid.uuid4())
        existing = {"id": tid, "title": "Pay vendor", "tags": []}
        with (
            _gate(ALLOWED),
            patch("robothor.crm.dal.get_task", return_value=existing),
            patch("robothor.crm.dal.update_task", return_value=True),
        ):
            result = await HANDLERS["update_task"](
                {"id": tid, "requiresHuman": True, "tags": ["wire"]}, CTX
            )
        assert result["success"] is True

    async def test_flip_unrestricted_when_key_absent(self):
        tid = str(uuid.uuid4())
        with (
            _gate(None),
            patch("robothor.crm.dal.get_task") as get,
            patch("robothor.crm.dal.update_task", return_value=True),
        ):
            result = await HANDLERS["update_task"]({"id": tid, "requiresHuman": True}, CTX)
        assert result["success"] is True
        get.assert_not_called()

    async def test_clearing_the_flag_is_never_gated(self):
        tid = str(uuid.uuid4())
        with (
            _gate(ALLOWED),
            patch("robothor.crm.dal.update_task", return_value=True),
        ):
            result = await HANDLERS["update_task"]({"id": tid, "requiresHuman": False}, CTX)
        assert result["success"] is True


@pytest.mark.parametrize("raw", ["payment", "a,b"])
def test_string_tags_are_tolerated(raw):
    # A model sometimes sends tags as one string; the guard must not crash on it.
    assert isinstance(crm_handlers._task_tags(raw), set)
