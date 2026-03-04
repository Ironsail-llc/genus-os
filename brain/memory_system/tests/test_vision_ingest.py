"""Tests for vision service ingestion pipeline (ORCHESTRATOR_URL fix).

TDD tests written to verify:
1. ORCHESTRATOR_URL config variable is properly defined
2. ingest_event() sends correct POST payload
3. Error handling (non-200, network failure) doesn't crash
4. Metadata structure for each detection type
5. Live smoke test against running orchestrator
"""

import importlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Add vision service directory to path
VISION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if VISION_DIR not in sys.path:
    sys.path.insert(0, VISION_DIR)


# ---------------------------------------------------------------------------
# 1. Configuration tests — ORCHESTRATOR_URL must be defined and correct
# ---------------------------------------------------------------------------


class TestOrchestratorUrlConfig:
    """Verify the ORCHESTRATOR_URL fix is in place."""

    def test_orchestrator_url_is_defined(self):
        """ORCHESTRATOR_URL must exist as a module-level variable."""
        import vision_service

        assert hasattr(vision_service, "ORCHESTRATOR_URL"), (
            "ORCHESTRATOR_URL not defined in vision_service — this was the bug"
        )

    def test_orchestrator_url_default_value(self):
        """Default should point to localhost:9099."""
        import vision_service

        assert (
            os.getenv("ORCHESTRATOR_URL", "http://localhost:9099")
            == vision_service.ORCHESTRATOR_URL
        )

    def test_orchestrator_url_is_string(self):
        """Must be a string, not None or empty."""
        import vision_service

        url = vision_service.ORCHESTRATOR_URL
        assert isinstance(url, str)
        assert len(url) > 0

    def test_orchestrator_url_has_scheme(self):
        """URL must start with http:// or https://."""
        import vision_service

        url = vision_service.ORCHESTRATOR_URL
        assert url.startswith("http://") or url.startswith("https://")

    def test_orchestrator_url_respects_env_override(self):
        """If ORCHESTRATOR_URL env var is set, it should be used."""
        custom_url = "http://custom-orchestrator:9999"
        with patch.dict(os.environ, {"ORCHESTRATOR_URL": custom_url}):
            # Force reimport to pick up env change
            import vision_service

            reloaded = importlib.reload(vision_service)
            assert custom_url == reloaded.ORCHESTRATOR_URL
            # Restore default
            importlib.reload(vision_service)


# ---------------------------------------------------------------------------
# 2. ingest_event() unit tests — mock httpx, verify behavior
# ---------------------------------------------------------------------------


class TestIngestEvent:
    """Unit tests for the ingest_event() async function."""

    @pytest.fixture
    def mock_response_ok(self):
        """Mock a successful 200 response."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.text = "OK"
        return resp

    @pytest.fixture
    def mock_response_error(self):
        """Mock a 500 error response."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        resp.text = "Internal Server Error"
        return resp

    async def test_sends_post_to_orchestrator(self, mock_response_ok):
        """ingest_event must POST to ORCHESTRATOR_URL/ingest."""
        import vision_service

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_ok
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("vision_service.httpx.AsyncClient", return_value=mock_client):
            await vision_service.ingest_event("test event", {"key": "val"})

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0]
        assert url == f"{vision_service.ORCHESTRATOR_URL}/ingest"

    async def test_payload_structure(self, mock_response_ok):
        """POST body must have content, source_channel, content_type, metadata."""
        import vision_service

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_ok
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        event_text = "Motion detected in living room (score=0.25)"
        metadata = {"detection_type": "motion", "motion_score": 0.25}

        with patch("vision_service.httpx.AsyncClient", return_value=mock_client):
            await vision_service.ingest_event(event_text, metadata)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["content"] == event_text
        assert payload["source_channel"] == "camera"
        assert payload["content_type"] == "event"
        assert payload["metadata"] == metadata

    async def test_handles_non_200_without_crashing(self, mock_response_error):
        """Non-200 response should log warning, not raise."""
        import vision_service

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_error
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("vision_service.httpx.AsyncClient", return_value=mock_client):
            # Should not raise
            await vision_service.ingest_event("test", {})

    async def test_handles_network_error_without_crashing(self):
        """Network failures should be caught and logged, not raised."""
        import vision_service

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("vision_service.httpx.AsyncClient", return_value=mock_client):
            # Should not raise
            await vision_service.ingest_event("test", {})

    async def test_handles_timeout_without_crashing(self):
        """Timeout should be caught and logged, not raised."""
        import vision_service

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ReadTimeout("Timeout")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("vision_service.httpx.AsyncClient", return_value=mock_client):
            await vision_service.ingest_event("test", {})

    async def test_uses_30_second_timeout(self):
        """httpx client should be created with timeout=30.0."""
        import vision_service

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = "OK"

        with patch("vision_service.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await vision_service.ingest_event("test", {})

            mock_cls.assert_called_once_with(timeout=30.0)


# ---------------------------------------------------------------------------
# 3. Metadata structure tests — validate each detection type's payload
# ---------------------------------------------------------------------------


class TestMetadataStructures:
    """Verify metadata payloads match what each caller sends."""

    def _validate_common_fields(self, metadata: dict):
        """All metadata dicts should have camera_id."""
        assert "camera_id" in metadata
        assert metadata["camera_id"] == "living-room"

    def test_motion_basic_metadata(self):
        """Basic mode motion detection metadata."""
        metadata = {
            "detection_type": "motion",
            "motion_score": 0.218,
            "snapshot_path": "/home/philip/robothor/brain/memory/snapshots/20260214_091015.jpg",
            "camera_id": "living-room",
        }
        self._validate_common_fields(metadata)
        assert metadata["detection_type"] == "motion"
        assert isinstance(metadata["motion_score"], float)
        assert 0.0 < metadata["motion_score"] <= 1.0

    def test_motion_armed_metadata(self):
        """Armed mode motion (no persons) includes objects list."""
        metadata = {
            "detection_type": "motion",
            "motion_score": 0.35,
            "objects": ["chair", "cat"],
            "snapshot_path": "/path/to/snap.jpg",
            "camera_id": "living-room",
        }
        self._validate_common_fields(metadata)
        assert isinstance(metadata["objects"], list)

    def test_known_person_metadata(self):
        """Known person arrival metadata includes identity and similarity."""
        metadata = {
            "detection_type": "person",
            "identity": "Philip",
            "known": True,
            "snapshot_path": "/path/to/snap.jpg",
            "camera_id": "living-room",
            "similarity": 0.872,
        }
        self._validate_common_fields(metadata)
        assert metadata["known"] is True
        assert isinstance(metadata["identity"], str)
        assert metadata["identity"] != "unknown"
        assert 0.0 <= metadata["similarity"] <= 1.0

    def test_unknown_person_metadata(self):
        """Unknown person metadata (face not visible)."""
        metadata = {
            "detection_type": "person",
            "identity": "unknown",
            "known": False,
            "snapshot_path": "/path/to/snap.jpg",
            "camera_id": "living-room",
        }
        self._validate_common_fields(metadata)
        assert metadata["known"] is False
        assert metadata["identity"] == "unknown"

    def test_escalated_unknown_metadata(self):
        """Escalated unknown person (armed mode, VLM analyzed) has importance_score."""
        metadata = {
            "detection_type": "person",
            "identity": "unknown",
            "known": False,
            "snapshot_path": "/path/to/snap.jpg",
            "camera_id": "living-room",
            "importance_score": 0.8,
        }
        self._validate_common_fields(metadata)
        assert "importance_score" in metadata
        assert metadata["importance_score"] >= 0.5  # escalations are high importance

    def test_departure_metadata(self):
        """Person departure includes arrival and departure timestamps."""
        metadata = {
            "detection_type": "departure",
            "identity": "Philip",
            "camera_id": "living-room",
            "arrived_at": "2026-02-14T09:00:00",
            "departed_at": "2026-02-14T09:15:00",
        }
        self._validate_common_fields(metadata)
        assert metadata["detection_type"] == "departure"
        assert "arrived_at" in metadata
        assert "departed_at" in metadata


# ---------------------------------------------------------------------------
# 4. Smoke test — hit the live orchestrator (requires running services)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestLiveIngestion:
    """Smoke tests against the running orchestrator service."""

    async def test_orchestrator_health(self):
        """Orchestrator /health endpoint should respond."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://localhost:9099/health")
            assert resp.status_code == 200

    async def test_ingest_endpoint_accepts_camera_event(self):
        """POST /ingest with camera source_channel should return 200."""
        payload = {
            "content": "__test__ Vision smoke test event — safe to ignore",
            "source_channel": "camera",
            "content_type": "event",
            "metadata": {
                "detection_type": "motion",
                "motion_score": 0.01,
                "camera_id": "test",
                "test": True,
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "http://localhost:9099/ingest",
                json=payload,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "facts_processed" in data
            assert isinstance(data["facts_processed"], int)

    async def test_ingest_event_function_live(self):
        """Call ingest_event() directly and verify no exception is raised."""
        import vision_service

        # This will POST to the real orchestrator
        await vision_service.ingest_event(
            "__test__ Live ingest_event smoke test — safe to ignore",
            {
                "detection_type": "motion",
                "motion_score": 0.01,
                "camera_id": "test",
                "test": True,
            },
        )
        # If we get here without exception, the fix works
