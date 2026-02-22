"""
Vision service — always-on background service with switchable detection modes.

Three modes:
  disarmed — Camera connected, health endpoint up, no processing.
  basic    — Smart detection: motion -> YOLO -> InsightFace -> alerts for
             unknown persons, async VLM follow-up.
  armed    — Same as basic + per-frame tracking (no motion gate).

Models (YOLO + InsightFace) are loaded at startup. Unknown person detection
triggers alerts within 2 seconds, with async VLM analysis following.

Mode is switchable at runtime via HTTP POST /mode.

On-demand endpoints (/detections, /identifications, /enroll, /look) work
regardless of mode.

Usage:
    from robothor.vision.service import VisionService
    service = VisionService()
    asyncio.run(service.run())

    # Or via CLI:
    robothor serve-vision
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np

from robothor.vision.alerts import AlertManager, TelegramAlert
from robothor.vision.detector import ObjectDetector, detect_motion
from robothor.vision.face import FaceRecognizer

logger = logging.getLogger(__name__)

# Valid modes
VALID_MODES = ("disarmed", "basic", "armed")


class CameraStream:
    """Manages an OpenCV RTSP video capture with reconnection."""

    def __init__(self, url: str):
        self.url = url
        self.cap: Any = None
        self._connect()

    def _connect(self) -> None:
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if self.cap.isOpened():
            logger.info("Camera connected: %s", self.url)
        else:
            logger.warning("Camera failed to connect: %s", self.url)

    def read(self) -> np.ndarray | None:
        """Read a single frame, reconnecting if necessary."""
        if self.cap is None or not self.cap.isOpened():
            self._connect()
            if not self.cap.isOpened():
                return None
        ret, frame = self.cap.read()
        if not ret:
            logger.warning("Frame read failed, reconnecting...")
            self._connect()
            return None
        return frame

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()


class VisionService:
    """Always-on vision service with mode-switchable detection.

    Encapsulates camera, detection models, face recognition, alerts,
    and the HTTP health/control server.
    """

    def __init__(
        self,
        rtsp_url: str | None = None,
        ollama_url: str | None = None,
        orchestrator_url: str | None = None,
        health_port: int | None = None,
        snapshot_dir: str | Path | None = None,
        face_data_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        camera_id: str | None = None,
        default_mode: str | None = None,
        capture_fps: float | None = None,
        motion_threshold: float | None = None,
        motion_cooldown: float | None = None,
        person_alert_cooldown: float | None = None,
        person_gone_timeout: int | None = None,
        alert_manager: AlertManager | None = None,
    ):
        # Connection URLs
        self.rtsp_url = rtsp_url or os.environ.get("RTSP_URL", "rtsp://localhost:8554/webcam")
        self.ollama_url = ollama_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self.orchestrator_url = orchestrator_url or os.environ.get("ORCHESTRATOR_URL", "http://localhost:9099")
        self.health_port = health_port or int(os.environ.get("VISION_HEALTH_PORT", "8600"))

        # Directories
        _default_state = Path(os.environ.get("ROBOTHOR_MEMORY_DIR", Path.home() / "robothor" / "memory"))
        self.snapshot_dir = Path(snapshot_dir or os.environ.get("SNAPSHOT_DIR", _default_state / "snapshots"))
        self.face_data_dir = Path(face_data_dir or os.environ.get("FACE_DATA_DIR", _default_state / "faces"))
        self.state_dir = Path(state_dir or os.environ.get("STATE_DIR", str(_default_state)))

        # Camera identification
        self.camera_id = camera_id or os.environ.get("VISION_CAMERA_ID", "camera-0")

        # Detection settings
        self.capture_fps = capture_fps or float(os.environ.get("CAPTURE_FPS", "1.0"))
        self.motion_threshold = motion_threshold or float(os.environ.get("MOTION_THRESHOLD", "0.15"))
        self.motion_cooldown = motion_cooldown or float(os.environ.get("MOTION_COOLDOWN", "30.0"))
        self.person_alert_cooldown = person_alert_cooldown or float(os.environ.get("PERSON_ALERT_COOLDOWN", "120.0"))
        self.person_gone_timeout = person_gone_timeout or int(os.environ.get("PERSON_GONE_TIMEOUT", "60"))

        # Mode
        self.default_mode = default_mode or os.environ.get("VISION_DEFAULT_MODE", "basic")
        self.current_mode: str = self.default_mode

        # Detection models
        self.detector = ObjectDetector()
        self.recognizer = FaceRecognizer(data_dir=self.face_data_dir)

        # Alerts
        self.alerts = alert_manager or AlertManager()
        if not self.alerts.handlers:
            # Auto-configure Telegram if token available
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                self.alerts.add_handler(TelegramAlert(bot_token=token, chat_id=chat_id))

        # State
        self.people_present: dict[str, dict] = {}
        self.last_detection_time: str | None = None
        self.last_detection_details: dict | None = None
        self.running = True
        self.start_time: str | None = None

        # Motion tracking
        self._prev_gray: np.ndarray | None = None
        self._last_motion_time: float = 0.0
        self._last_motion_score: float = 0.0
        self._last_person_alert_time: float = 0.0
        self._unknown_counter = 0

    # ── Mode Management ──────────────────────────────────────────

    def _mode_file(self) -> Path:
        return self.state_dir / "vision_mode.txt"

    def load_mode(self) -> str:
        """Load persisted mode from disk, or return default."""
        f = self._mode_file()
        if f.exists():
            mode = f.read_text().strip()
            if mode in VALID_MODES:
                return mode
        return self.default_mode

    def save_mode(self, mode: str) -> None:
        """Persist mode to disk so it survives restarts."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._mode_file().write_text(mode)

    def set_mode(self, mode: str) -> str:
        """Switch the service mode at runtime."""
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}. Valid: {VALID_MODES}")

        old = self.current_mode
        self.current_mode = mode
        self.save_mode(mode)
        logger.info("Mode changed: %s -> %s", old, mode)

        if mode in ("basic", "armed"):
            self.detector._ensure_loaded()
            self.recognizer._ensure_loaded()

        if mode == "disarmed":
            self.people_present.clear()

        return mode

    # ── Snapshot & Ingestion ─────────────────────────────────────

    def save_snapshot(self, frame: np.ndarray) -> str:
        """Save a frame to the date-organized snapshot directory."""
        now = datetime.now()
        day_dir = self.snapshot_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / now.strftime("%H%M%S.jpg")
        cv2.imwrite(str(path), frame)
        return str(path)

    async def ingest_event(self, event_text: str, metadata: dict) -> None:
        """Post a vision event to the orchestrator for ingestion."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.orchestrator_url}/ingest",
                    json={
                        "content": event_text,
                        "source_channel": "camera",
                        "content_type": "event",
                        "metadata": metadata,
                    },
                )
                if resp.status_code == 200:
                    logger.info("Event ingested: %s", event_text[:80])
                else:
                    logger.warning("Ingestion failed (%d): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Ingestion error: %s", e)

    async def publish_event(self, event_type: str, payload: dict) -> None:
        """Publish to event bus (best-effort)."""
        try:
            from robothor.events.bus import publish
            publish("vision", event_type, payload, source="vision_service")
        except Exception as e:
            logger.debug("Event bus publish failed (non-fatal): %s", e)

    # ── VLM Analysis ─────────────────────────────────────────────

    async def analyze_vlm(self, frame: np.ndarray, prompt: str = "Describe what you see in this image in detail.") -> str:
        """Send a frame to a vision LLM for analysis."""
        _, buf = cv2.imencode(".jpg", frame)
        img_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        model = os.environ.get("ROBOTHOR_VISION_MODEL", "llama3.2-vision:11b")

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a vision system. Describe what you see clearly and concisely. Note any people, objects, and notable details."},
                {"role": "user", "content": prompt, "images": [img_b64]},
            ],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 1024, "num_gpu": 999},
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(f"{self.ollama_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    # ── Alert Helpers ────────────────────────────────────────────

    async def _alert_unknown(self, frame: np.ndarray, snapshot_path: str, message: str) -> None:
        """Send instant alert for unknown person, then fire-and-forget VLM follow-up."""
        # Encode frame as JPEG for alert
        _, buf = cv2.imencode(".jpg", frame)
        image_bytes = buf.tobytes()

        await self.alerts.send(
            "unknown_person",
            message,
            image_bytes=image_bytes,
            metadata={"snapshot_path": snapshot_path, "camera_id": self.camera_id},
        )

        # Fire-and-forget VLM analysis
        asyncio.create_task(self._vlm_followup(frame.copy(), snapshot_path))

    async def _vlm_followup(self, frame: np.ndarray, snapshot_path: str) -> None:
        """Async VLM follow-up: analyze unknown person and send description."""
        try:
            description = await self.analyze_vlm(
                frame,
                "An unknown person has been detected. Describe this person: appearance, clothing, estimated age, what they appear to be doing.",
            )
        except Exception:
            description = "VLM analysis unavailable"

        await self.alerts.send(
            "vlm_analysis",
            f"AI Analysis: {description}",
            metadata={"snapshot_path": snapshot_path},
        )

        await self.ingest_event(
            f"Unknown person detected at {self.camera_id}. {description}",
            {
                "detection_type": "person",
                "identity": "unknown",
                "known": False,
                "snapshot_path": snapshot_path,
                "camera_id": self.camera_id,
                "importance_score": 0.8,
            },
        )

    def _get_unknown_id(self) -> str:
        """Generate a unique unknown person ID."""
        self._unknown_counter += 1
        return f"unknown_{self._unknown_counter:03d}"

    # ── Frame Processing ─────────────────────────────────────────

    async def process_frame_basic(self, frame: np.ndarray) -> None:
        """Basic mode: motion -> YOLO -> face ID -> alert unknown."""
        motion_detected, motion_score, self._prev_gray = detect_motion(
            frame, self._prev_gray, self.motion_threshold
        )
        self._last_motion_score = motion_score

        if not motion_detected:
            return

        now_ts = time.time()
        now_str = datetime.now().isoformat()

        detections = self.detector.detect(frame)
        persons = [d for d in detections if d["class"] == "person"]

        if not persons:
            if now_ts - self._last_motion_time >= self.motion_cooldown:
                self._last_motion_time = now_ts
                self.last_detection_time = now_str
                object_classes = list({d["class"] for d in detections})
                snapshot_path = self.save_snapshot(frame)
                logger.info("Motion detected (score=%.3f, objects=%s)", motion_score, object_classes or "none")
                await self.ingest_event(
                    f"Motion detected at {self.camera_id} (score={motion_score})"
                    + (f", objects: {', '.join(object_classes)}" if object_classes else ""),
                    {
                        "detection_type": "motion",
                        "motion_score": motion_score,
                        "objects": object_classes,
                        "snapshot_path": snapshot_path,
                        "camera_id": self.camera_id,
                    },
                )
            return

        self.last_detection_time = now_str
        self.last_detection_details = {"persons": len(persons), "total_objects": len(detections)}

        faces = self.recognizer.detect(frame)

        for face in faces:
            name, sim = self.recognizer.match(face["embedding"])
            if name:
                if name not in self.people_present:
                    snapshot_path = self.save_snapshot(frame)
                    self.people_present[name] = {
                        "last_seen": now_str,
                        "arrived_at": now_str,
                        "snapshot": snapshot_path,
                    }
                    logger.info("Known person detected: %s (sim=%.3f)", name, sim)
                    await self.ingest_event(
                        f"{name} detected at {self.camera_id}",
                        {
                            "detection_type": "person",
                            "identity": name,
                            "known": True,
                            "snapshot_path": snapshot_path,
                            "camera_id": self.camera_id,
                            "similarity": round(sim, 3),
                        },
                    )
                else:
                    self.people_present[name]["last_seen"] = now_str
            else:
                if now_ts - self._last_person_alert_time < self.person_alert_cooldown:
                    logger.debug("Unknown person detected but within alert cooldown")
                    continue
                self._last_person_alert_time = now_ts
                unknown_id = self._get_unknown_id()
                snapshot_path = self.save_snapshot(frame)
                self.people_present[unknown_id] = {
                    "last_seen": now_str,
                    "arrived_at": now_str,
                    "snapshot": snapshot_path,
                    "unknown": True,
                }
                logger.info("UNKNOWN person detected (id=%s) — sending alert", unknown_id)
                await self._alert_unknown(frame, snapshot_path, f"Unknown person detected at {self.camera_id}")
                await self.publish_event("vision.person_unknown", {
                    "snapshot": snapshot_path, "camera": self.camera_id,
                })

        # Person detected but no face visible
        if persons and not faces:
            if now_ts - self._last_person_alert_time < self.person_alert_cooldown:
                return
            self._last_person_alert_time = now_ts
            snapshot_path = self.save_snapshot(frame)
            logger.info("Person detected (no face visible) — sending alert")
            await self._alert_unknown(frame, snapshot_path, f"Person detected at {self.camera_id} (face not visible)")
            await self.ingest_event(
                f"Person detected at {self.camera_id} (face not visible)",
                {
                    "detection_type": "person",
                    "identity": "unknown",
                    "known": False,
                    "snapshot_path": snapshot_path,
                    "camera_id": self.camera_id,
                },
            )

        # Departure tracking
        self._track_departures(now_str)

    async def process_frame_armed(self, frame: np.ndarray) -> None:
        """Armed mode: YOLO + face ID on every motion frame, full tracking."""
        now = datetime.now()
        now_str = now.isoformat()

        motion_detected, motion_score, self._prev_gray = detect_motion(
            frame, self._prev_gray, self.motion_threshold
        )
        self._last_motion_score = motion_score

        if not motion_detected and not self.people_present:
            return

        detections = self.detector.detect(frame)
        persons = [d for d in detections if d["class"] == "person"]

        # Non-person motion with cooldown
        if motion_detected and not persons:
            now_ts = time.time()
            if now_ts - self._last_motion_time >= self.motion_cooldown:
                self._last_motion_time = now_ts
                object_classes = list({d["class"] for d in detections})
                snapshot_path = self.save_snapshot(frame)
                logger.info("Motion detected (score=%.3f, objects=%s)", motion_score, object_classes or "none")
                await self.ingest_event(
                    f"Motion detected at {self.camera_id} (score={motion_score})"
                    + (f", objects: {', '.join(object_classes)}" if object_classes else ""),
                    {
                        "detection_type": "motion",
                        "motion_score": motion_score,
                        "objects": object_classes,
                        "snapshot_path": snapshot_path,
                        "camera_id": self.camera_id,
                    },
                )

        if not persons and not self.people_present:
            return

        seen_this_frame: set[str] = set()

        if persons:
            self.last_detection_time = now_str
            self.last_detection_details = {"persons": len(persons), "total_objects": len(detections)}

            faces = self.recognizer.detect(frame)

            for face in faces:
                name, sim = self.recognizer.match(face["embedding"])
                if name:
                    seen_this_frame.add(name)
                    if name not in self.people_present:
                        snapshot_path = self.save_snapshot(frame)
                        self.people_present[name] = {
                            "last_seen": now_str,
                            "arrived_at": now_str,
                            "snapshot": snapshot_path,
                        }
                        logger.info("Person appeared: %s (sim=%.3f)", name, sim)
                        await self.ingest_event(
                            f"{name} detected at {self.camera_id}",
                            {
                                "detection_type": "person",
                                "identity": name,
                                "known": True,
                                "snapshot_path": snapshot_path,
                                "camera_id": self.camera_id,
                                "similarity": round(sim, 3),
                            },
                        )
                    else:
                        self.people_present[name]["last_seen"] = now_str
                else:
                    unknown_id = self._get_unknown_id()
                    snapshot_path = self.save_snapshot(frame)
                    self.people_present[unknown_id] = {
                        "last_seen": now_str,
                        "arrived_at": now_str,
                        "snapshot": snapshot_path,
                        "unknown": True,
                    }
                    seen_this_frame.add(unknown_id)
                    logger.info("UNKNOWN person detected (id=%s) — sending alert", unknown_id)
                    await self._alert_unknown(frame, snapshot_path, f"Unknown person detected at {self.camera_id}")

            if persons and not faces:
                key = "_person_no_face"
                if key not in self.people_present:
                    snapshot_path = self.save_snapshot(frame)
                    self.people_present[key] = {
                        "last_seen": now_str,
                        "arrived_at": now_str,
                        "snapshot": snapshot_path,
                    }
                    seen_this_frame.add(key)
                    logger.info("Person detected (no face visible) — sending alert")
                    await self._alert_unknown(frame, snapshot_path, f"Person detected at {self.camera_id} (face not visible)")
                    await self.ingest_event(
                        f"Person detected at {self.camera_id} (face not visible)",
                        {
                            "detection_type": "person",
                            "identity": "unknown",
                            "known": False,
                            "snapshot_path": snapshot_path,
                            "camera_id": self.camera_id,
                        },
                    )
                else:
                    self.people_present[key]["last_seen"] = now_str
                    seen_this_frame.add(key)

        # Departure tracking (armed uses seen_this_frame)
        departed = []
        for identity, info in list(self.people_present.items()):
            if identity not in seen_this_frame:
                last_seen = datetime.fromisoformat(info["last_seen"])
                if (now - last_seen).total_seconds() > self.person_gone_timeout:
                    departed.append(identity)

        for identity in departed:
            info = self.people_present.pop(identity)
            if not identity.startswith("_") and not identity.startswith("unknown_"):
                logger.info("Person departed: %s", identity)
                await self.ingest_event(
                    f"{identity} left {self.camera_id}",
                    {
                        "detection_type": "departure",
                        "identity": identity,
                        "camera_id": self.camera_id,
                        "arrived_at": info.get("arrived_at"),
                        "departed_at": now_str,
                    },
                )

    def _track_departures(self, now_str: str) -> None:
        """Track people who have left (basic mode)."""
        now = datetime.now()
        departed = []
        for identity, info in list(self.people_present.items()):
            if identity.startswith("_"):
                continue
            last_seen = datetime.fromisoformat(info["last_seen"])
            if (now - last_seen).total_seconds() > self.person_gone_timeout:
                departed.append(identity)

        for identity in departed:
            self.people_present.pop(identity)
            if not identity.startswith("unknown_"):
                logger.info("Person departed: %s", identity)

    # ── Enrollment ───────────────────────────────────────────────

    async def enroll_person(self, name: str, num_frames: int = 5) -> dict:
        """Capture multiple frames and enroll a person's face."""
        camera = CameraStream(self.rtsp_url)
        embeddings = []

        for _ in range(num_frames * 3):
            frame = camera.read()
            if frame is None:
                continue

            faces = self.recognizer.detect(frame)
            if faces:
                best_face = max(faces, key=lambda f: f["det_score"])
                embeddings.append(best_face["embedding"])
                logger.info("Enrollment frame %d/%d captured for %s", len(embeddings), num_frames, name)
                if len(embeddings) >= num_frames:
                    break

            await asyncio.sleep(0.5)

        camera.release()

        if len(embeddings) < 2:
            return {"success": False, "error": f"Could only capture {len(embeddings)} face(s), need at least 2"}

        ok = self.recognizer.enroll(name, embeddings)
        if not ok:
            return {"success": False, "error": "Enrollment failed"}

        snapshot_path = None
        cam = CameraStream(self.rtsp_url)
        frame = cam.read()
        cam.release()
        if frame is not None:
            snapshot_path = self.save_snapshot(frame)

        logger.info("Enrolled %s with %d face samples", name, len(embeddings))
        return {"success": True, "name": name, "samples": len(embeddings), "snapshot_path": snapshot_path}

    # ── HTTP Server ──────────────────────────────────────────────

    def _json_response(self, status: str, body: dict) -> bytes:
        """Build a minimal HTTP response."""
        body_str = json.dumps(body)
        return f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body_str)}\r\n\r\n{body_str}".encode()

    async def handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle HTTP requests on the service endpoint."""
        data = await reader.read(8192)
        request = data.decode(errors="replace")

        first_line = request.split("\n")[0] if request else ""
        parts = first_line.split()
        method = parts[0] if len(parts) >= 1 else ""
        path = parts[1] if len(parts) >= 2 else ""

        req_body = ""
        body_start = request.find("\r\n\r\n")
        if body_start >= 0:
            req_body = request[body_start + 4:]

        try:
            resp = await self._route_request(method, path, req_body)
        except Exception as e:
            logger.error("Request handler error: %s", e, exc_info=True)
            resp = self._json_response("500 Internal Server Error", {"error": str(e)})

        writer.write(resp)
        await writer.drain()
        writer.close()

    async def _route_request(self, method: str, path: str, body: str) -> bytes:
        """Route an HTTP request to the appropriate handler."""
        if path == "/health":
            return self._json_response("200 OK", {
                "running": self.running,
                "mode": self.current_mode,
                "started_at": self.start_time,
                "people_present": [k for k in self.people_present if not k.startswith("_")],
                "last_detection": self.last_detection_time,
                "last_detection_details": self.last_detection_details,
                "last_motion_score": self._last_motion_score,
                "motion_threshold": self.motion_threshold,
                "enrolled_faces": self.recognizer.enrolled_names,
                "models_loaded": self.detector.loaded and self.recognizer.loaded,
                "camera_url": self.rtsp_url,
            })

        if path == "/mode" and method == "GET":
            return self._json_response("200 OK", {"mode": self.current_mode, "valid_modes": list(VALID_MODES)})

        if path == "/mode" and method == "POST":
            try:
                req_data = json.loads(body)
                new_mode = self.set_mode(req_data["mode"])
                return self._json_response("200 OK", {"mode": new_mode, "message": f"Switched to {new_mode}"})
            except (json.JSONDecodeError, KeyError):
                return self._json_response("400 Bad Request", {"error": 'Send JSON: {"mode": "basic|armed|disarmed"}'})
            except ValueError as e:
                return self._json_response("400 Bad Request", {"error": str(e)})

        if path == "/detections":
            camera = CameraStream(self.rtsp_url)
            frame = camera.read()
            camera.release()
            if frame is not None:
                detections = self.detector.detect(frame)
                snapshot_path = self.save_snapshot(frame)
                return self._json_response("200 OK", {"detections": detections, "snapshot_path": snapshot_path})
            return self._json_response("503 Service Unavailable", {"detections": [], "error": "Camera unavailable"})

        if path == "/identifications":
            camera = CameraStream(self.rtsp_url)
            frame = camera.read()
            camera.release()
            if frame is not None:
                faces = self.recognizer.detect(frame)
                results = []
                for face in faces:
                    name, sim = self.recognizer.match(face["embedding"])
                    results.append({
                        "name": name or "unknown",
                        "known": name is not None,
                        "similarity": round(sim, 3),
                        "det_score": round(face["det_score"], 3),
                    })
                return self._json_response("200 OK", {
                    "identifications": results,
                    "people_present": [k for k in self.people_present if not k.startswith("_")],
                })
            return self._json_response("503 Service Unavailable", {"identifications": [], "error": "Camera unavailable"})

        if path == "/enroll" and method == "POST":
            try:
                req_data = json.loads(body)
                name = req_data.get("name", "")
                if not name:
                    return self._json_response("400 Bad Request", {"error": "Name is required"})
                result = await self.enroll_person(name)
                status = "200 OK" if result.get("success") else "422 Unprocessable Entity"
                return self._json_response(status, result)
            except json.JSONDecodeError:
                return self._json_response("400 Bad Request", {"error": "Invalid JSON"})

        if path == "/look" and method == "POST":
            try:
                req_data = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError:
                req_data = {}
            prompt = req_data.get("prompt", "Describe what you see in this image in detail.")
            camera = CameraStream(self.rtsp_url)
            frame = camera.read()
            camera.release()
            if frame is not None:
                snapshot_path = self.save_snapshot(frame)
                try:
                    description = await self.analyze_vlm(frame, prompt)
                    return self._json_response("200 OK", {
                        "description": description,
                        "snapshot_path": snapshot_path,
                        "prompt": prompt,
                    })
                except Exception as e:
                    return self._json_response("502 Bad Gateway", {"error": f"VLM analysis failed: {e}", "snapshot_path": snapshot_path})
            return self._json_response("503 Service Unavailable", {"error": "Camera unavailable"})

        return self._json_response("404 Not Found", {"error": "Not found", "endpoints": [
            "GET /health", "GET /mode", "POST /mode", "GET /detections",
            "GET /identifications", "POST /enroll", "POST /look",
        ]})

    # ── Detection Loop ───────────────────────────────────────────

    async def detection_loop(self) -> None:
        """Main loop — dispatches frames based on current mode."""
        camera = CameraStream(self.rtsp_url)
        interval = 1.0 / self.capture_fps
        reconnect_wait = 5.0

        logger.info("Detection loop starting (%.1f FPS, mode=%s)", self.capture_fps, self.current_mode)

        while self.running:
            t0 = time.time()

            if self.current_mode == "disarmed":
                await asyncio.sleep(interval)
                continue

            frame = camera.read()
            if frame is None:
                logger.warning("No frame, waiting %.0fs to reconnect...", reconnect_wait)
                await asyncio.sleep(reconnect_wait)
                camera = CameraStream(self.rtsp_url)
                continue

            try:
                if self.current_mode == "basic":
                    await self.process_frame_basic(frame)
                elif self.current_mode == "armed":
                    await self.process_frame_armed(frame)
            except Exception as e:
                logger.error("Frame processing error: %s", e, exc_info=True)

            elapsed = time.time() - t0
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        camera.release()
        logger.info("Detection loop stopped")

    # ── Run ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the vision service (HTTP server + detection loop)."""
        def handle_signal(sig, _frame):
            logger.info("Signal %s received, shutting down...", sig)
            self.running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        self.start_time = datetime.now().isoformat()
        self.current_mode = self.load_mode()
        logger.info("Vision Service starting (mode=%s)...", self.current_mode)

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.face_data_dir.mkdir(parents=True, exist_ok=True)

        server = await asyncio.start_server(self.handle_request, "127.0.0.1", self.health_port)
        logger.info("HTTP endpoint listening on port %d", self.health_port)

        try:
            await self.detection_loop()
        finally:
            server.close()
            await server.wait_closed()
            logger.info("Vision service stopped")


def main() -> None:
    """CLI entry point for the vision service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    service = VisionService()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
