"""Tests for robothor.vision.service — VisionService and CameraStream."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from robothor.vision.alerts import AlertManager
from robothor.vision.service import VALID_MODES, CameraStream, VisionService

pytestmark = pytest.mark.vision

# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def tmp_dirs(tmp_path):
    """Create temporary directories for service state."""
    snapshot_dir = tmp_path / "snapshots"
    face_dir = tmp_path / "faces"
    state_dir = tmp_path / "state"
    snapshot_dir.mkdir()
    face_dir.mkdir()
    state_dir.mkdir()
    return snapshot_dir, face_dir, state_dir


@pytest.fixture
def service(tmp_dirs, monkeypatch):
    """Create a VisionService with temp directories, no real camera."""
    snapshot_dir, face_dir, state_dir = tmp_dirs
    # Ensure no Telegram auto-configuration from env
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return VisionService(
        rtsp_url="rtsp://localhost:8554/test",
        snapshot_dir=snapshot_dir,
        face_data_dir=face_dir,
        state_dir=state_dir,
        camera_id="test-camera",
        default_mode="disarmed",
        capture_fps=1.0,
    )


@pytest.fixture
def fake_frame():
    """Create a fake BGR frame (100x100 black image)."""
    return np.zeros((100, 100, 3), dtype=np.uint8)


# ─── Mode Management ────────────────────────────────────────────


class TestModeManagement:
    def test_valid_modes(self):
        assert VALID_MODES == ("disarmed", "basic", "armed")

    def test_initial_mode(self, service):
        assert service.current_mode == "disarmed"

    def test_set_mode_basic(self, service):
        with (
            patch.object(service.detector, "_ensure_loaded"),
            patch.object(service.recognizer, "_ensure_loaded"),
        ):
            result = service.set_mode("basic")
        assert result == "basic"
        assert service.current_mode == "basic"

    def test_set_mode_armed(self, service):
        with (
            patch.object(service.detector, "_ensure_loaded"),
            patch.object(service.recognizer, "_ensure_loaded"),
        ):
            result = service.set_mode("armed")
        assert result == "armed"
        assert service.current_mode == "armed"

    def test_set_mode_disarmed_clears_people(self, service):
        service.people_present = {"Alice": {"last_seen": "2024-01-01"}}
        service.set_mode("disarmed")
        assert service.people_present == {}

    def test_set_mode_invalid(self, service):
        with pytest.raises(ValueError, match="Invalid mode"):
            service.set_mode("turbo")

    def test_mode_persistence(self, service, tmp_dirs):
        _, _, state_dir = tmp_dirs
        service.set_mode("disarmed")
        service.save_mode("armed")
        loaded = service.load_mode()
        assert loaded == "armed"

    def test_load_mode_default_when_no_file(self, service):
        assert service.load_mode() == "disarmed"

    def test_load_mode_ignores_invalid(self, service, tmp_dirs):
        _, _, state_dir = tmp_dirs
        mode_file = state_dir / "vision_mode.txt"
        mode_file.write_text("invalid_mode")
        assert service.load_mode() == "disarmed"


# ─── Snapshot ────────────────────────────────────────────────────


class TestSnapshot:
    def test_save_snapshot(self, service, fake_frame, tmp_dirs):
        snapshot_dir = tmp_dirs[0]
        mock_cv2 = MagicMock()
        with patch("robothor.vision.service._get_cv2", return_value=mock_cv2):
            path = service.save_snapshot(fake_frame)
        assert "snapshots" in path or snapshot_dir.name in path
        mock_cv2.imwrite.assert_called_once()


# ─── Unknown ID Generator ───────────────────────────────────────


class TestUnknownId:
    def test_generates_sequential(self, service):
        id1 = service._get_unknown_id()
        id2 = service._get_unknown_id()
        assert id1 == "unknown_001"
        assert id2 == "unknown_002"

    def test_counter_increments(self, service):
        for _ in range(10):
            service._get_unknown_id()
        assert service._unknown_counter == 10


# ─── Event Ingestion ────────────────────────────────────────────


class TestIngestion:
    @pytest.mark.asyncio
    async def test_ingest_event_posts_to_orchestrator(self, service):
        with patch("robothor.vision.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client_cls.return_value = mock_client

            await service.ingest_event("test event", {"key": "value"})

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/ingest" in call_args[0][0]
            body = call_args[1]["json"]
            assert body["content"] == "test event"
            assert body["source_channel"] == "camera"

    @pytest.mark.asyncio
    async def test_ingest_event_handles_failure(self, service):
        with patch("robothor.vision.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=Exception("Network error"))
            mock_client_cls.return_value = mock_client

            # Should not raise
            await service.ingest_event("test event", {})


# ─── Event Bus Publishing ───────────────────────────────────────


class TestPublishEvent:
    @pytest.mark.asyncio
    async def test_publish_event_calls_bus(self, service):
        mock_pub = MagicMock()
        with patch.dict("sys.modules", {"robothor.events.bus": MagicMock(publish=mock_pub)}):
            await service.publish_event("vision.test", {"data": "value"})
            mock_pub.assert_called_once_with(
                "vision", "vision.test", {"data": "value"}, source="vision_service"
            )

    @pytest.mark.asyncio
    async def test_publish_event_handles_import_error(self, service):
        # The method catches all exceptions including ImportError
        with patch.dict("sys.modules", {"robothor.events.bus": None}):
            # Should not raise
            await service.publish_event("vision.test", {})


# ─── VLM Analysis ───────────────────────────────────────────────


class TestVLM:
    @pytest.mark.asyncio
    async def test_analyze_vlm_calls_ollama(self, service, fake_frame):
        mock_cv2 = MagicMock()
        mock_cv2.imencode.return_value = (True, np.array([1, 2, 3], dtype=np.uint8))
        with (
            patch("robothor.vision.service._get_cv2", return_value=mock_cv2),
            patch("robothor.vision.service.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"message": {"content": "I see a room"}}
            mock_resp.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await service.analyze_vlm(fake_frame, "What do you see?")
            assert result == "I see a room"


# ─── HTTP Server Routing ────────────────────────────────────────


class TestHTTPRouting:
    @pytest.mark.asyncio
    async def test_health_endpoint(self, service):
        resp = await service._route_request("GET", "/health", "")
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["running"] is True
        assert body["mode"] == "disarmed"
        assert "people_present" in body
        assert "camera_url" in body

    @pytest.mark.asyncio
    async def test_mode_get(self, service):
        resp = await service._route_request("GET", "/mode", "")
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["mode"] == "disarmed"
        assert body["valid_modes"] == ["disarmed", "basic", "armed"]

    @pytest.mark.asyncio
    async def test_mode_post_valid(self, service):
        resp = await service._route_request("POST", "/mode", '{"mode": "disarmed"}')
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["mode"] == "disarmed"

    @pytest.mark.asyncio
    async def test_mode_post_invalid(self, service):
        resp = await service._route_request("POST", "/mode", '{"mode": "turbo"}')
        assert b"400" in resp

    @pytest.mark.asyncio
    async def test_mode_post_bad_json(self, service):
        resp = await service._route_request("POST", "/mode", "not json")
        assert b"400" in resp

    @pytest.mark.asyncio
    async def test_404_unknown_path(self, service):
        resp = await service._route_request("GET", "/nonexistent", "")
        assert b"404" in resp
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert "endpoints" in body

    @pytest.mark.asyncio
    async def test_detections_no_camera(self, service):
        with patch("robothor.vision.service.CameraStream") as mock_cam_cls:
            mock_cam = MagicMock()
            mock_cam.read.return_value = None
            mock_cam_cls.return_value = mock_cam
            resp = await service._route_request("GET", "/detections", "")
        assert b"503" in resp

    @pytest.mark.asyncio
    async def test_detections_with_frame(self, service, fake_frame):
        with (
            patch("robothor.vision.service.CameraStream") as mock_cam_cls,
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "cat", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
        ):
            mock_cam = MagicMock()
            mock_cam.read.return_value = fake_frame
            mock_cam_cls.return_value = mock_cam
            resp = await service._route_request("GET", "/detections", "")
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert len(body["detections"]) == 1
        assert body["detections"][0]["class"] == "cat"

    @pytest.mark.asyncio
    async def test_identifications_no_camera(self, service):
        with patch("robothor.vision.service.CameraStream") as mock_cam_cls:
            mock_cam = MagicMock()
            mock_cam.read.return_value = None
            mock_cam_cls.return_value = mock_cam
            resp = await service._route_request("GET", "/identifications", "")
        assert b"503" in resp

    @pytest.mark.asyncio
    async def test_enroll_no_name(self, service):
        resp = await service._route_request("POST", "/enroll", '{"name": ""}')
        assert b"400" in resp

    @pytest.mark.asyncio
    async def test_enroll_bad_json(self, service):
        resp = await service._route_request("POST", "/enroll", "not json")
        assert b"400" in resp

    @pytest.mark.asyncio
    async def test_look_no_camera(self, service):
        with patch("robothor.vision.service.CameraStream") as mock_cam_cls:
            mock_cam = MagicMock()
            mock_cam.read.return_value = None
            mock_cam_cls.return_value = mock_cam
            resp = await service._route_request("POST", "/look", "")
        assert b"503" in resp


# ─── Frame Processing (Basic Mode) ──────────────────────────────


class TestBasicMode:
    @pytest.mark.asyncio
    async def test_no_motion_skips_processing(self, service, fake_frame):
        with patch(
            "robothor.vision.service.detect_motion",
            return_value=(False, 0.01, np.zeros((100, 100))),
        ):
            await service.process_frame_basic(fake_frame)
        # No detections run — quick exit
        assert service.last_detection_time is None

    @pytest.mark.asyncio
    async def test_motion_no_person_logs(self, service, fake_frame):
        service._last_motion_time = 0  # ensure no cooldown
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.25, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "cat", "confidence": 0.8, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "ingest_event", new_callable=AsyncMock) as mock_ingest,
        ):
            await service.process_frame_basic(fake_frame)
        mock_ingest.assert_called_once()
        assert "Motion" in mock_ingest.call_args[0][0]

    @pytest.mark.asyncio
    async def test_known_person_detected(self, service, fake_frame):
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=("Alice", 0.92)),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "ingest_event", new_callable=AsyncMock),
        ):
            await service.process_frame_basic(fake_frame)
        assert "Alice" in service.people_present

    @pytest.mark.asyncio
    async def test_unknown_person_triggers_alert(self, service, fake_frame):
        service._last_person_alert_time = 0  # no cooldown
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=(None, 0.2)),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "_alert_unknown", new_callable=AsyncMock) as mock_alert,
            patch.object(service, "publish_event", new_callable=AsyncMock),
        ):
            await service.process_frame_basic(fake_frame)
        mock_alert.assert_called_once()
        assert any(k.startswith("unknown_") for k in service.people_present)

    @pytest.mark.asyncio
    async def test_unknown_person_cooldown(self, service, fake_frame):
        service._last_person_alert_time = time.time()  # just alerted
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=(None, 0.2)),
            patch.object(service, "_alert_unknown", new_callable=AsyncMock) as mock_alert,
        ):
            await service.process_frame_basic(fake_frame)
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_person_no_face_triggers_alert(self, service, fake_frame):
        service._last_person_alert_time = 0
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(service.recognizer, "detect", return_value=[]),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "_alert_unknown", new_callable=AsyncMock) as mock_alert,
            patch.object(service, "ingest_event", new_callable=AsyncMock),
        ):
            await service.process_frame_basic(fake_frame)
        mock_alert.assert_called_once()
        assert "face not visible" in mock_alert.call_args[0][2]


# ─── Frame Processing (Armed Mode) ──────────────────────────────


class TestArmedMode:
    @pytest.mark.asyncio
    async def test_no_motion_no_people_skips(self, service, fake_frame):
        with patch(
            "robothor.vision.service.detect_motion",
            return_value=(False, 0.01, np.zeros((100, 100))),
        ):
            await service.process_frame_armed(fake_frame)
        assert service.last_detection_time is None

    @pytest.mark.asyncio
    async def test_person_detection_armed(self, service, fake_frame):
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=("Bob", 0.88)),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "ingest_event", new_callable=AsyncMock),
        ):
            await service.process_frame_armed(fake_frame)
        assert "Bob" in service.people_present


# ─── Departure Tracking ─────────────────────────────────────────


class TestDepartureTracking:
    def test_departure_removes_stale(self, service):
        old_time = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
        service.people_present = {
            "Alice": {"last_seen": old_time, "arrived_at": old_time},
        }
        service.person_gone_timeout = 60
        service._track_departures(datetime.now(tz=UTC).isoformat())
        assert "Alice" not in service.people_present

    def test_departure_keeps_recent(self, service):
        recent = datetime.now(tz=UTC).isoformat()
        service.people_present = {
            "Alice": {"last_seen": recent, "arrived_at": recent},
        }
        service._track_departures(datetime.now(tz=UTC).isoformat())
        assert "Alice" in service.people_present

    def test_departure_skips_internal_keys(self, service):
        """Internal keys (starting with _) are skipped by departure tracking."""
        old_time = (datetime.now(tz=UTC) - timedelta(seconds=120)).isoformat()
        service.people_present = {
            "_person_no_face": {"last_seen": old_time, "arrived_at": old_time},
        }
        service.person_gone_timeout = 60
        service._track_departures(datetime.now(tz=UTC).isoformat())
        # Internal keys are skipped (continue), so they stay in people_present
        assert "_person_no_face" in service.people_present


# ─── Configuration ───────────────────────────────────────────────


class TestConfiguration:
    def test_default_config(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        svc = VisionService()
        assert svc.rtsp_url == "rtsp://localhost:8554/webcam"
        assert svc.health_port == 8600
        assert svc.capture_fps == 1.0

    def test_custom_config(self, tmp_dirs, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        snapshot_dir, face_dir, state_dir = tmp_dirs
        svc = VisionService(
            rtsp_url="rtsp://10.0.0.1:8554/cam",
            health_port=9999,
            snapshot_dir=snapshot_dir,
            face_data_dir=face_dir,
            state_dir=state_dir,
            camera_id="front-door",
            capture_fps=5.0,
        )
        assert svc.rtsp_url == "rtsp://10.0.0.1:8554/cam"
        assert svc.health_port == 9999
        assert svc.camera_id == "front-door"
        assert svc.capture_fps == 5.0

    def test_env_var_config(self, tmp_dirs, monkeypatch):
        snapshot_dir, face_dir, state_dir = tmp_dirs
        monkeypatch.setenv("RTSP_URL", "rtsp://envhost:8554/cam")
        monkeypatch.setenv("VISION_CAMERA_ID", "env-camera")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        svc = VisionService(snapshot_dir=snapshot_dir, face_data_dir=face_dir, state_dir=state_dir)
        assert svc.rtsp_url == "rtsp://envhost:8554/cam"
        assert svc.camera_id == "env-camera"


# ─── Alert Integration ──────────────────────────────────────────


class TestAlertIntegration:
    def test_no_handlers_by_default(self, tmp_dirs, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        snapshot_dir, face_dir, state_dir = tmp_dirs
        svc = VisionService(snapshot_dir=snapshot_dir, face_data_dir=face_dir, state_dir=state_dir)
        assert len(svc.alerts.handlers) == 0

    def test_telegram_auto_configured(self, tmp_dirs, monkeypatch):
        snapshot_dir, face_dir, state_dir = tmp_dirs
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        svc = VisionService(snapshot_dir=snapshot_dir, face_data_dir=face_dir, state_dir=state_dir)
        assert len(svc.alerts.handlers) == 1

    def test_custom_alert_manager(self, tmp_dirs, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        snapshot_dir, face_dir, state_dir = tmp_dirs
        custom = AlertManager()
        svc = VisionService(
            snapshot_dir=snapshot_dir,
            face_data_dir=face_dir,
            state_dir=state_dir,
            alert_manager=custom,
        )
        assert svc.alerts is custom


# ─── CameraStream ───────────────────────────────────────────────


class TestCameraStream:
    def _make_mock_cv2(self, mock_cap):
        """Create a mock cv2 module with common attributes."""
        mock_cv2 = MagicMock()
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.CAP_FFMPEG = 1800
        mock_cv2.CAP_PROP_BUFFERSIZE = 38
        return mock_cv2

    def test_init_connects(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cv2 = self._make_mock_cv2(mock_cap)
        with patch("robothor.vision.service._get_cv2", return_value=mock_cv2):
            cam = CameraStream("rtsp://test:8554/webcam")
            assert cam.url == "rtsp://test:8554/webcam"
            mock_cv2.VideoCapture.assert_called_once()

    def test_read_returns_frame(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, np.zeros((100, 100, 3)))
        mock_cv2 = self._make_mock_cv2(mock_cap)
        with patch("robothor.vision.service._get_cv2", return_value=mock_cv2):
            cam = CameraStream("rtsp://test:8554/webcam")
            frame = cam.read()
            assert frame is not None

    def test_read_returns_none_on_failure(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (False, None)
        mock_cv2 = self._make_mock_cv2(mock_cap)
        with patch("robothor.vision.service._get_cv2", return_value=mock_cv2):
            cam = CameraStream("rtsp://test:8554/webcam")
            frame = cam.read()
            assert frame is None

    def test_release(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cv2 = self._make_mock_cv2(mock_cap)
        with patch("robothor.vision.service._get_cv2", return_value=mock_cv2):
            cam = CameraStream("rtsp://test:8554/webcam")
            cam.release()
            mock_cap.release.assert_called_once()


# ─── Lazy cv2 Import ──────────────────────────────────────────


class TestLazyCv2Import:
    def test_import_without_cv2(self):
        """Importing the module should succeed even without opencv installed."""
        # The module is already imported (it uses lazy _get_cv2), so we test
        # that _get_cv2 raises a helpful ImportError when cv2 is missing.
        import builtins

        from robothor.vision.service import _get_cv2

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("No module named 'cv2'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="pip install robothor\\[vision\\]"):
                _get_cv2()


# ─── Photo-Based Enrollment ──────────────────────────────────


class TestEnrollFromImage:
    def test_enroll_from_valid_images(self, service, tmp_dirs):
        face_dir = tmp_dirs[1]
        # Create a fake image file
        img_path = str(face_dir / "test.jpg")
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = fake_frame
        embedding = np.random.randn(512).astype(np.float32)
        with (
            patch("robothor.vision.service._get_cv2", return_value=mock_cv2),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[{"bbox": [0, 0, 50, 50], "embedding": embedding, "det_score": 0.95}],
            ),
            patch.object(service.recognizer, "_ensure_loaded"),
            patch.object(service.recognizer, "enroll", return_value=True),
            patch("pathlib.Path.exists", return_value=True),
        ):
            result = service.enroll_from_image("Alice", [img_path])
        assert result["success"] is True
        assert result["name"] == "Alice"
        assert result["samples"] == 1

    def test_enroll_from_missing_file(self, service):
        with (
            patch("robothor.vision.service._get_cv2", return_value=MagicMock()),
            patch.object(service.recognizer, "_ensure_loaded"),
        ):
            result = service.enroll_from_image("Alice", ["/nonexistent/photo.jpg"])
        assert result["success"] is False
        assert "No usable face" in result["error"]

    def test_enroll_from_no_face_detected(self, service, tmp_dirs):
        face_dir = tmp_dirs[1]
        img_path = str(face_dir / "noface.jpg")
        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        with (
            patch("robothor.vision.service._get_cv2", return_value=mock_cv2),
            patch.object(service.recognizer, "detect", return_value=[]),
            patch.object(service.recognizer, "_ensure_loaded"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            result = service.enroll_from_image("Alice", [img_path])
        assert result["success"] is False
        assert any("No face" in e for e in result["details"])

    @pytest.mark.asyncio
    async def test_enroll_from_image_http_route(self, service):
        with patch.object(
            service,
            "enroll_from_image",
            return_value={"success": True, "name": "Alice", "samples": 3},
        ):
            resp = await service._route_request(
                "POST",
                "/enroll-from-image",
                '{"name": "Alice", "image_paths": ["/tmp/a.jpg"]}',
            )
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["success"] is True

    @pytest.mark.asyncio
    async def test_enroll_from_image_missing_name(self, service):
        resp = await service._route_request(
            "POST", "/enroll-from-image", '{"name": "", "image_paths": ["/tmp/a.jpg"]}'
        )
        assert b"400" in resp

    @pytest.mark.asyncio
    async def test_enroll_from_image_missing_paths(self, service):
        resp = await service._route_request(
            "POST", "/enroll-from-image", '{"name": "Alice", "image_paths": []}'
        )
        assert b"400" in resp


# ─── Enrolled Faces Listing ──────────────────────────────────


class TestEnrolledEndpoint:
    @pytest.mark.asyncio
    async def test_enrolled_list(self, service):
        service.recognizer.enrolled = {"Alice": np.ones(512), "Bob": np.ones(512)}
        resp = await service._route_request("GET", "/enrolled", "")
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["count"] == 2
        assert "Alice" in body["enrolled_faces"]


# ─── Unenroll Endpoint ──────────────────────────────────────


class TestUnenrollEndpoint:
    @pytest.mark.asyncio
    async def test_unenroll_existing(self, service):
        service.recognizer.enrolled = {"Alice": np.ones(512)}
        with patch.object(service.recognizer, "unenroll", return_value=True):
            resp = await service._route_request("POST", "/unenroll", '{"name": "Alice"}')
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["success"] is True

    @pytest.mark.asyncio
    async def test_unenroll_not_found(self, service):
        with patch.object(service.recognizer, "unenroll", return_value=False):
            resp = await service._route_request("POST", "/unenroll", '{"name": "Nobody"}')
        assert b"404" in resp


# ─── Alert Suppression ──────────────────────────────────────


class TestAlertSuppression:
    def test_on_known_person_seen_suppresses(self, service):
        assert service.alerts_suppressed is False
        service._on_known_person_seen()
        assert service.alerts_suppressed is True
        assert service.last_known_person_seen > 0

    def test_on_known_person_seen_idempotent(self, service):
        service._on_known_person_seen()
        first_time = service.last_known_person_seen
        time.sleep(0.01)
        service._on_known_person_seen()
        # Still suppressed, time updated
        assert service.alerts_suppressed is True
        assert service.last_known_person_seen >= first_time

    @pytest.mark.asyncio
    async def test_auto_arm_after_delay(self, service):
        service.alerts_suppressed = True
        service.auto_arm_delay = 1  # 1 second for testing
        service.last_known_person_seen = time.time() - 2  # 2 seconds ago
        with patch.object(service, "publish_event", new_callable=AsyncMock) as mock_pub:
            await service._check_auto_arm()
        assert service.alerts_suppressed is False
        mock_pub.assert_called_once()
        assert mock_pub.call_args[0][0] == "vision.auto_armed"

    @pytest.mark.asyncio
    async def test_auto_arm_not_triggered_when_recent(self, service):
        service.alerts_suppressed = True
        service.auto_arm_delay = 1800
        service.last_known_person_seen = time.time()  # just now
        await service._check_auto_arm()
        assert service.alerts_suppressed is True

    @pytest.mark.asyncio
    async def test_suppression_skips_alert_basic_mode(self, service, fake_frame):
        service.alerts_suppressed = True
        service._last_person_alert_time = 0
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=(None, 0.2)),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "_alert_unknown", new_callable=AsyncMock) as mock_alert,
            patch.object(service, "publish_event", new_callable=AsyncMock),
            patch.object(service, "_check_auto_arm", new_callable=AsyncMock),
        ):
            await service.process_frame_basic(fake_frame)
        # Alert should NOT fire because suppressed
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_known_person_triggers_suppression_in_basic(self, service, fake_frame):
        service.alerts_suppressed = False
        with (
            patch(
                "robothor.vision.service.detect_motion",
                return_value=(True, 0.3, np.zeros((100, 100))),
            ),
            patch.object(
                service.detector,
                "detect",
                return_value=[{"class": "person", "confidence": 0.9, "bbox": [0, 0, 50, 50]}],
            ),
            patch.object(
                service.recognizer,
                "detect",
                return_value=[
                    {"bbox": [0, 0, 50, 50], "embedding": np.ones(512), "det_score": 0.95}
                ],
            ),
            patch.object(service.recognizer, "match", return_value=("Alice", 0.92)),
            patch.object(service, "save_snapshot", return_value="/tmp/snap.jpg"),
            patch.object(service, "ingest_event", new_callable=AsyncMock),
            patch.object(service, "publish_event", new_callable=AsyncMock),
            patch.object(service, "_check_auto_arm", new_callable=AsyncMock),
        ):
            await service.process_frame_basic(fake_frame)
        assert service.alerts_suppressed is True


# ─── Suppression Persistence ────────────────────────────────


class TestSuppressionPersistence:
    def test_save_and_load(self, service, tmp_dirs):
        service.alerts_suppressed = True
        service.last_known_person_seen = 1234567890.0
        service.save_suppression()
        # Reset and reload
        service.alerts_suppressed = False
        service.last_known_person_seen = 0.0
        service.load_suppression()
        assert service.alerts_suppressed is True
        assert service.last_known_person_seen == 1234567890.0

    def test_load_missing_file(self, service):
        # No file exists — should not crash
        service.load_suppression()
        assert service.alerts_suppressed is False

    @pytest.mark.asyncio
    async def test_suppression_http_get(self, service):
        service.alerts_suppressed = True
        service.last_known_person_seen = 12345.0
        resp = await service._route_request("GET", "/suppression", "")
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["alerts_suppressed"] is True

    @pytest.mark.asyncio
    async def test_suppression_http_post(self, service):
        resp = await service._route_request("POST", "/suppression", '{"suppressed": true}')
        body = json.loads(resp.decode().split("\r\n\r\n")[1])
        assert body["alerts_suppressed"] is True
        assert service.alerts_suppressed is True
