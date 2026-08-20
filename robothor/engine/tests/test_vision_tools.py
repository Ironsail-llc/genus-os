"""Tests for vision tool handlers — face enrollment linked to the person
identity graph via ``face_identities`` (Task 7, Unified Identity Context).

The vision service (robothor/vision/service.py) stays DB-free; these
handlers proxy to it over HTTP through the shared service client (mocked at
the ``service_client.httpx.AsyncClient`` seam) and separately read/write
``face_identities`` (mocked at the ``_get_conn`` seam, see
test_identity_tools.py).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.vision import HANDLERS
from robothor.engine.tools.service_client import reset_circuit_breakers

CTX = ToolContext(agent_id="test", tenant_id="test-tenant")


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "https://vision.test/x"),
    )


def _mock_client(*, get: httpx.Response | None = None, post: httpx.Response | None = None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _request(method: str, url: str, **kwargs):
        return get if method.upper() == "GET" else post

    client.request = AsyncMock(side_effect=_request)
    return client


def _mock_db(cursor_mock):
    """Create a mock _get_conn that yields a connection with the given cursor."""
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _fake_conn():
        yield mock_conn

    return _fake_conn, mock_conn


@contextmanager
def _fail_conn():
    raise Exception("db down")
    yield  # noqa: RET503  (unreachable, keeps this a generator)


# ─── enroll_face ────────────────────────────────────────────────────────


class TestEnrollFaceUnlinked:
    @pytest.mark.asyncio
    async def test_enroll_without_person_args_upserts_null_person(self):
        """No person_id/person_name: the vision enrollment still succeeds AND
        a face_identities row is upserted with person_id=NULL, so the label
        is registered for who_is_here's join even though it's unlinked."""
        resp = _mock_response({"success": True, "name": "front-door", "samples": 3})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["enroll_face"]({"name": "front-door"}, CTX)

        assert result["success"] is True
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "INSERT INTO face_identities" in sql
        assert params == ("test-tenant", "front-door", None, "")
        assert result["identity"] == {"linked": False, "person_id": None, "display_name": None}

    @pytest.mark.asyncio
    async def test_enroll_service_failure_does_not_upsert(self):
        """vision service reports success=False (e.g. too few samples): no
        face was actually enrolled, so no face_identities row is written."""
        resp = _mock_response({"success": False, "error": "not enough samples"})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["enroll_face"]({"name": "front-door"}, CTX)

        assert result["success"] is False
        cur.execute.assert_not_called()
        assert "identity" not in result

    @pytest.mark.asyncio
    async def test_missing_name_returns_error_before_any_http_or_db_call(self):
        result = await HANDLERS["enroll_face"]({}, CTX)
        assert "error" in result


class TestEnrollFaceLinked:
    @pytest.mark.asyncio
    async def test_enroll_with_person_id_links_and_derives_display_name(self):
        resp = _mock_response({"success": True, "name": "front-door", "samples": 3})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)
        person = {"id": "person-1", "name": {"firstName": "Alice", "lastName": "Rivera"}}

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
            patch("robothor.crm.dal.get_person", return_value=person),
        ):
            result = await HANDLERS["enroll_face"](
                {"name": "front-door", "person_id": "person-1"}, CTX
            )

        sql, params = cur.execute.call_args.args
        assert params == ("test-tenant", "front-door", "person-1", "Alice Rivera")
        assert result["identity"] == {
            "linked": True,
            "person_id": "person-1",
            "display_name": "Alice Rivera",
        }

    @pytest.mark.asyncio
    async def test_enroll_with_unknown_person_id_falls_back_unlinked(self):
        """person_id given but not found in crm_people (wrong tenant, typo,
        stale id): don't write a dangling reference -- enroll unlinked."""
        resp = _mock_response({"success": True, "name": "front-door", "samples": 3})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
            patch("robothor.crm.dal.get_person", return_value=None),
        ):
            result = await HANDLERS["enroll_face"](
                {"name": "front-door", "person_id": "does-not-exist"}, CTX
            )

        sql, params = cur.execute.call_args.args
        assert params[2] is None  # person_id column
        assert result["identity"]["linked"] is False

    @pytest.mark.asyncio
    async def test_enroll_with_person_name_only_stores_free_text_unlinked(self):
        """person_name alone (no person_id) stores a free-text display_name
        but does NOT resolve/link a person_id -- resolving a bare name to a
        crm_people row is ambiguous and out of scope for a one-shot enroll."""
        resp = _mock_response({"success": True, "name": "front-door", "samples": 3})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["enroll_face"](
                {"name": "front-door", "person_name": "Guest Bob"}, CTX
            )

        sql, params = cur.execute.call_args.args
        assert params == ("test-tenant", "front-door", None, "Guest Bob")
        assert result["identity"]["linked"] is False
        assert result["identity"]["display_name"] == "Guest Bob"

    @pytest.mark.asyncio
    async def test_enroll_upsert_db_failure_degrades_without_failing_enrollment(self):
        """face_identities missing/DB down: the vision enrollment itself
        already succeeded -- the tool result must still report success, with
        identity.linked=False rather than raising or erroring the call."""
        resp = _mock_response({"success": True, "name": "front-door", "samples": 3})
        client = _mock_client(post=resp)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", _fail_conn),
        ):
            result = await HANDLERS["enroll_face"]({"name": "front-door"}, CTX)

        assert result["success"] is True
        assert result["identity"] == {"linked": False}


class TestEnrollFaceFromImage:
    @pytest.mark.asyncio
    async def test_links_person_like_enroll_face(self):
        resp = _mock_response(
            {"success": True, "name": "front-door", "samples": 2, "images_provided": 2}
        )
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)
        person = {"id": "person-1", "name": {"firstName": "Alice", "lastName": "Rivera"}}

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
            patch("robothor.crm.dal.get_person", return_value=person),
        ):
            result = await HANDLERS["enroll_face_from_image"](
                {
                    "name": "front-door",
                    "image_paths": ["/tmp/a.jpg"],
                    "person_id": "person-1",
                },
                CTX,
            )

        cur.execute.assert_called_once()
        assert result["identity"]["linked"] is True

    @pytest.mark.asyncio
    async def test_service_failure_does_not_upsert(self):
        resp = _mock_response({"success": False, "error": "No usable face embeddings found"})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["enroll_face_from_image"](
                {"name": "front-door", "image_paths": ["/tmp/a.jpg"]}, CTX
            )

        assert result["success"] is False
        cur.execute.assert_not_called()


# ─── unenroll_face ──────────────────────────────────────────────────────


class TestUnenrollFace:
    @pytest.mark.asyncio
    async def test_unenroll_deletes_face_identity_row(self):
        resp = _mock_response({"success": True, "message": "Unenrolled front-door"})
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["unenroll_face"]({"name": "front-door"}, CTX)

        assert result["success"] is True
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "DELETE FROM face_identities" in sql
        assert params == ("test-tenant", "front-door")

    @pytest.mark.asyncio
    async def test_unenroll_service_404_does_not_touch_db(self):
        """Service reports the name wasn't enrolled (404): the shared client
        maps it to a structured error before the DELETE runs -- no
        partial/incorrect delete, and no exception escapes the tool."""
        resp = _mock_response(
            {"success": False, "error": "front-door not enrolled"}, status_code=404
        )
        client = _mock_client(post=resp)
        cur = MagicMock()
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["unenroll_face"]({"name": "front-door"}, CTX)

        assert result["error"] == "vision service error (HTTP 404)"
        cur.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unenroll_db_error_does_not_fail_the_tool_result(self):
        resp = _mock_response({"success": True, "message": "Unenrolled front-door"})
        client = _mock_client(post=resp)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", _fail_conn),
        ):
            result = await HANDLERS["unenroll_face"]({"name": "front-door"}, CTX)

        assert result["success"] is True


# ─── who_is_here ────────────────────────────────────────────────────────


class TestWhoIsHere:
    @pytest.mark.asyncio
    async def test_no_people_present_returns_empty_identifications(self):
        resp = _mock_response(
            {"people_present": [], "running": True, "mode": "armed", "last_detection": None}
        )
        client = _mock_client(get=resp)

        with patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client):
            result = await HANDLERS["who_is_here"]({}, CTX)

        assert result["people_present"] == []
        assert result["identifications"] == []

    @pytest.mark.asyncio
    async def test_joins_linked_label_with_person_and_role_unlinked_label_omits_them(self):
        resp = _mock_response(
            {
                "people_present": ["alice-front", "unknown-1"],
                "running": True,
                "mode": "armed",
                "last_detection": "2026-07-16T12:00:00",
            }
        )
        client = _mock_client(get=resp)
        cur = MagicMock()
        cur.fetchall.return_value = [("alice-front", "person-1", "Alice Rivera", "member")]
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["who_is_here"]({}, CTX)

        assert result["people_present"] == ["alice-front", "unknown-1"]
        ids_by_label = {i["label"]: i for i in result["identifications"]}

        alice = ids_by_label["alice-front"]
        assert alice["person_id"] == "person-1"
        assert alice["display_name"] == "Alice Rivera"
        assert alice["role"] == "member"
        assert alice["verified"] is False

        unknown = ids_by_label["unknown-1"]
        assert unknown["verified"] is False
        assert "person_id" not in unknown
        assert "role" not in unknown

    @pytest.mark.asyncio
    async def test_label_linked_but_no_tenant_users_role_omits_role_key(self):
        resp = _mock_response({"people_present": ["alice-front"], "running": True, "mode": "armed"})
        client = _mock_client(get=resp)
        cur = MagicMock()
        cur.fetchall.return_value = [("alice-front", "person-1", "Alice Rivera", None)]
        fake_conn, _ = _mock_db(cur)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", fake_conn),
        ):
            result = await HANDLERS["who_is_here"]({}, CTX)

        entry = result["identifications"][0]
        assert entry["person_id"] == "person-1"
        assert "role" not in entry

    @pytest.mark.asyncio
    async def test_join_db_error_degrades_to_unjoined_identifications(self):
        """DB errors during the post-join must NOT break who_is_here's
        result -- it degrades to unjoined identifications, not a failure."""
        resp = _mock_response({"people_present": ["alice-front"], "running": True, "mode": "armed"})
        client = _mock_client(get=resp)

        with (
            patch("robothor.engine.tools.service_client.httpx.AsyncClient", return_value=client),
            patch("robothor.engine.tools.handlers.vision._get_conn", _fail_conn),
        ):
            result = await HANDLERS["who_is_here"]({}, CTX)

        assert result["people_present"] == ["alice-front"]
        assert result["identifications"] == [{"label": "alice-front", "verified": False}]
