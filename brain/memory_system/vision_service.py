#!/usr/bin/env python3
"""
Robothor Vision Service — Always-on background service with switchable modes.

One service, always running, three levels:

  disarmed  — Camera connected, health endpoint up, no processing.
  basic     — Smart detection: motion → YOLO → InsightFace → instant Telegram
              alerts for unknown persons, async VLM follow-up.
  armed     — Same as basic + additional tracking (for manual override/debug).

Models (YOLO + InsightFace) are loaded at startup regardless of mode (~306MB).
Unknown person detection sends a snapshot to Telegram within 2 seconds.
VLM analysis (llama3.2-vision) follows asynchronously 15-30s later.

Mode is switchable at runtime via HTTP:
  POST /mode {"mode": "basic"}

On-demand endpoints (/detections, /identifications, /enroll) always work
regardless of mode — they capture a fresh frame and run the requested
analysis on demand.

Systemd: robothor-vision.service (starts on boot, Restart=always)
"""

import asyncio
import base64
import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path

import cv2
import event_bus
import httpx
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vision_service")

# ============== Configuration ==============

RTSP_URL = os.getenv("RTSP_URL", "rtsp://localhost:8554/webcam")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:9099")
HEALTH_PORT = int(os.getenv("VISION_HEALTH_PORT", "8600"))
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "/home/philip/robothor/brain/memory/snapshots"))
FACE_DATA_DIR = Path(os.getenv("FACE_DATA_DIR", "/home/philip/robothor/brain/memory/faces"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/home/philip/robothor/brain/memory"))

# Telegram alerts
PHILIP_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7636850023")

# Detection settings
CAPTURE_FPS = float(os.getenv("CAPTURE_FPS", "1.0"))
YOLO_CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.5"))
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.45"))
PERSON_GONE_TIMEOUT = int(os.getenv("PERSON_GONE_TIMEOUT", "60"))

# Motion detection
MOTION_THRESHOLD = float(os.getenv("MOTION_THRESHOLD", "0.15"))
MOTION_COOLDOWN = float(os.getenv("MOTION_COOLDOWN", "30.0"))
PERSON_ALERT_COOLDOWN = float(os.getenv("PERSON_ALERT_COOLDOWN", "120.0"))

# Valid modes
VALID_MODES = ("disarmed", "basic", "armed")
DEFAULT_MODE = os.getenv("VISION_DEFAULT_MODE", "basic")

# ============== Global State ==============

# Current mode
current_mode: str = DEFAULT_MODE
# People currently present: {identity: {"last_seen": ..., "arrived_at": ..., ...}}
people_present: dict[str, dict] = {}
# Last detection info
last_detection_time: str | None = None
last_detection_details: dict | None = None
# Running flag
running = True
# Model references (lazy-loaded)
yolo_model = None
face_app = None
_models_loaded = False
# Enrolled face embeddings: {name: np.array}
enrolled_faces: dict[str, np.ndarray] = {}
# Motion detection state
_prev_gray: np.ndarray | None = None
_last_motion_time: float = 0.0
_last_motion_score: float = 0.0
# Service start time
_start_time: str | None = None
# Unknown person counter
_unknown_counter = 0
# Last person alert time (for cooldown in basic mode)
_last_person_alert_time: float = 0.0


# ============== Mode Persistence ==============


def _mode_file() -> Path:
    return STATE_DIR / "vision_mode.txt"


def load_mode() -> str:
    """Load persisted mode from disk, or return default."""
    f = _mode_file()
    if f.exists():
        mode = f.read_text().strip()
        if mode in VALID_MODES:
            return mode
    return DEFAULT_MODE


def save_mode(mode: str):
    """Persist mode to disk so it survives restarts."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _mode_file().write_text(mode)


def set_mode(mode: str) -> str:
    """Switch the service mode at runtime.

    Returns the new mode. If 'armed', triggers lazy model loading.
    """
    global current_mode
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}. Valid: {VALID_MODES}")

    old = current_mode
    current_mode = mode
    save_mode(mode)
    logger.info("Mode changed: %s -> %s", old, mode)

    # Models are always loaded at startup, but ensure just in case
    if mode in ("basic", "armed"):
        ensure_models_loaded()

    # Clear people state when disarming (clean slate)
    if mode == "disarmed":
        people_present.clear()

    return mode


# ============== Motion Detection ==============


def detect_motion(frame: np.ndarray) -> tuple[bool, float]:
    """Detect motion via frame differencing.

    Returns (motion_detected, score) where score is the fraction of
    pixels that changed significantly.
    """
    global _prev_gray

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)

    if _prev_gray is None:
        _prev_gray = gray
        return False, 0.0

    delta = cv2.absdiff(_prev_gray, gray)
    _prev_gray = gray

    _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
    score = float(np.count_nonzero(thresh)) / float(thresh.size)

    return score >= MOTION_THRESHOLD, round(score, 4)


# ============== Model Loading (Lazy) ==============


def ensure_models_loaded():
    """Load YOLO and InsightFace models if not already loaded.

    Called lazily when switching to armed mode or when on-demand
    endpoints need them. Models stay in memory once loaded.
    """
    global yolo_model, face_app, _models_loaded
    if _models_loaded:
        return

    logger.info("Loading detection models...")

    # YOLO
    if yolo_model is None:
        try:
            from ultralytics import YOLO

            yolo_model = YOLO("yolov8n.pt")
            logger.info("YOLOv8-nano loaded")
        except Exception as e:
            logger.error("Failed to load YOLO: %s", e)

    # InsightFace
    if face_app is None:
        try:
            from insightface.app import FaceAnalysis

            face_app = FaceAnalysis(
                name="buffalo_l",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
            face_app.prepare(ctx_id=0, det_size=(640, 640))
            logger.info("InsightFace buffalo_l loaded")
        except Exception as e:
            logger.warning("InsightFace failed to load (%s), face recognition disabled", e)

    _models_loaded = True


def load_enrolled_faces():
    """Load enrolled face embeddings from disk."""
    global enrolled_faces
    FACE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    face_file = FACE_DATA_DIR / "enrolled_faces.json"
    if face_file.exists():
        with open(face_file) as f:
            data = json.load(f)
        for name, emb_list in data.items():
            enrolled_faces[name] = np.array(emb_list, dtype=np.float32)
        logger.info("Loaded %d enrolled faces", len(enrolled_faces))
    else:
        logger.info("No enrolled faces found")


def save_enrolled_faces():
    """Save enrolled face embeddings to disk."""
    FACE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    face_file = FACE_DATA_DIR / "enrolled_faces.json"
    data = {name: emb.tolist() for name, emb in enrolled_faces.items()}
    with open(face_file, "w") as f:
        json.dump(data, f)
    logger.info("Saved %d enrolled faces", len(enrolled_faces))


# ============== Camera ==============


class CameraStream:
    """Manages an OpenCV RTSP video capture with reconnection."""

    def __init__(self, url: str):
        self.url = url
        self.cap = None
        self._connect()

    def _connect(self):
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

    def release(self):
        if self.cap is not None:
            self.cap.release()


# ============== Detection Functions ==============


def detect_objects(frame: np.ndarray) -> list[dict]:
    """Run YOLOv8 detection on a frame."""
    if yolo_model is None:
        return []
    results = yolo_model(frame, verbose=False, conf=YOLO_CONFIDENCE)
    detections = []
    for r in results:
        for box in r.boxes:
            detections.append(
                {
                    "class": r.names[int(box.cls[0])],
                    "confidence": round(float(box.conf[0]), 3),
                    "bbox": [round(float(x), 1) for x in box.xyxy[0].tolist()],
                }
            )
    return detections


def detect_faces(frame: np.ndarray) -> list[dict]:
    """Run InsightFace on a frame to detect and embed faces."""
    if face_app is None:
        return []
    faces = face_app.get(frame)
    return [
        {
            "bbox": face.bbox.tolist(),
            "embedding": face.normed_embedding,
            "det_score": float(face.det_score),
        }
        for face in faces
    ]


def match_face(embedding: np.ndarray) -> tuple[str | None, float]:
    """Match a face embedding against enrolled faces."""
    if not enrolled_faces:
        return None, 0.0

    best_name = None
    best_sim = 0.0
    for name, enrolled_emb in enrolled_faces.items():
        sim = float(
            np.dot(embedding, enrolled_emb)
            / (np.linalg.norm(embedding) * np.linalg.norm(enrolled_emb))
        )
        if sim > best_sim:
            best_sim = sim
            best_name = name

    if best_sim >= FACE_MATCH_THRESHOLD:
        return best_name, best_sim
    return None, best_sim


# ============== State Tracking & Ingestion ==============


def save_snapshot(frame: np.ndarray) -> str:
    """Save a frame to the date-organized snapshot directory."""
    now = datetime.now()
    day_dir = SNAPSHOT_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / now.strftime("%H%M%S.jpg")
    cv2.imwrite(str(path), frame)
    return str(path)


async def ingest_event(event_text: str, metadata: dict):
    """Post a vision event to the orchestrator for ingestion."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/ingest",
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


async def send_telegram_photo(image_path: str, caption: str = "") -> bool:
    """Send a snapshot to Philip via Telegram immediately."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            with open(image_path, "rb") as photo:
                resp = await client.post(
                    url,
                    data={"chat_id": PHILIP_CHAT_ID, "caption": caption[:1024]},
                    files={"photo": ("snapshot.jpg", photo, "image/jpeg")},
                )
            if resp.status_code == 200:
                logger.info("Telegram photo sent: %s", caption[:60])
                return True
            else:
                logger.warning("Telegram photo failed (%d): %s", resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        logger.error("Telegram photo error: %s", e)
        return False


async def send_telegram_text(text: str) -> bool:
    """Send a text message to Philip via Telegram."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={"chat_id": PHILIP_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
            if resp.status_code == 200:
                logger.info("Telegram text sent: %s", text[:60])
                return True
            else:
                logger.warning("Telegram text failed (%d): %s", resp.status_code, resp.text[:200])
                return False
    except Exception as e:
        logger.error("Telegram text error: %s", e)
        return False


async def escalate_unknown_vlm(frame: np.ndarray, snapshot_path: str):
    """Async VLM follow-up: analyze unknown person and send description to Telegram.

    Runs as a fire-and-forget task after the snapshot has already been sent.
    """
    try:
        description = await analyze_image_vlm(
            frame,
            "An unknown person has been detected. Describe this person: appearance, clothing, estimated age, what they appear to be doing.",
        )
    except Exception:
        description = "VLM analysis unavailable"

    # Send VLM description as follow-up Telegram text
    await send_telegram_text(f"🔍 <b>AI Analysis:</b> {description}")

    # Also ingest to memory
    await ingest_event(
        f"Unknown person detected in living room. {description}",
        {
            "detection_type": "person",
            "identity": "unknown",
            "known": False,
            "snapshot_path": snapshot_path,
            "camera_id": "living-room",
            "importance_score": 0.8,
        },
    )


def get_unknown_id() -> str:
    """Generate a unique unknown person ID."""
    global _unknown_counter
    _unknown_counter += 1
    return f"unknown_{_unknown_counter:03d}"


# ============== Frame Processing Per Mode ==============


async def process_frame_basic(frame: np.ndarray):
    """Basic mode: smart detection pipeline.

    Motion → YOLO (50ms) → person? → InsightFace (200ms) → known/unknown?
    Unknown: instant Telegram photo → async VLM follow-up.
    Known: log arrival, no alert.
    No persons: log motion with cooldown.
    """
    global _last_motion_time, _last_motion_score, _last_person_alert_time
    global last_detection_time, last_detection_details

    motion_detected, motion_score = detect_motion(frame)
    _last_motion_score = motion_score

    if not motion_detected:
        return

    now_ts = time.time()
    now_str = datetime.now().isoformat()

    # Run YOLO on motion frames
    detections = detect_objects(frame)
    persons = [d for d in detections if d["class"] == "person"]

    if not persons:
        # Non-person motion — log with cooldown
        if now_ts - _last_motion_time >= MOTION_COOLDOWN:
            _last_motion_time = now_ts
            last_detection_time = now_str
            object_classes = list({d["class"] for d in detections})
            snapshot_path = save_snapshot(frame)
            logger.info(
                "Motion detected (score=%.3f, objects=%s)", motion_score, object_classes or "none"
            )
            await ingest_event(
                f"Motion detected in living room (score={motion_score})"
                + (f", objects: {', '.join(object_classes)}" if object_classes else ""),
                {
                    "detection_type": "motion",
                    "motion_score": motion_score,
                    "objects": object_classes,
                    "snapshot_path": snapshot_path,
                    "camera_id": "living-room",
                },
            )
        return

    # Person(s) detected — run face identification
    last_detection_time = now_str
    last_detection_details = {"persons": len(persons), "total_objects": len(detections)}

    faces = detect_faces(frame)

    for face in faces:
        name, sim = match_face(face["embedding"])
        if name:
            # Known person — log arrival if new, no alert
            if name not in people_present:
                snapshot_path = save_snapshot(frame)
                people_present[name] = {
                    "last_seen": now_str,
                    "arrived_at": now_str,
                    "snapshot": snapshot_path,
                }
                logger.info("Known person detected: %s (sim=%.3f)", name, sim)
                await ingest_event(
                    f"{name} detected in living room",
                    {
                        "detection_type": "person",
                        "identity": name,
                        "known": True,
                        "snapshot_path": snapshot_path,
                        "camera_id": "living-room",
                        "similarity": round(sim, 3),
                    },
                )
            else:
                people_present[name]["last_seen"] = now_str
        else:
            # Unknown face — alert with cooldown
            if now_ts - _last_person_alert_time < PERSON_ALERT_COOLDOWN:
                logger.debug("Unknown person detected but within alert cooldown")
                continue
            _last_person_alert_time = now_ts
            unknown_id = get_unknown_id()
            snapshot_path = save_snapshot(frame)
            people_present[unknown_id] = {
                "last_seen": now_str,
                "arrived_at": now_str,
                "snapshot": snapshot_path,
                "unknown": True,
            }
            logger.info("UNKNOWN person detected (id=%s) — sending alert", unknown_id)
            # Immediate: send snapshot to Telegram
            await send_telegram_photo(snapshot_path, "⚠️ Unknown person detected in living room")
            # Fire-and-forget: VLM analysis + follow-up message
            asyncio.create_task(escalate_unknown_vlm(frame.copy(), snapshot_path))
            # Dual-write: publish to event bus
            event_bus.publish(
                "vision",
                "vision.person_unknown",
                {
                    "snapshot": snapshot_path,
                    "camera": "living-room",
                },
                source="vision_service",
            )

    # Person detected but no face visible
    if persons and not faces:
        if now_ts - _last_person_alert_time < PERSON_ALERT_COOLDOWN:
            return
        _last_person_alert_time = now_ts
        snapshot_path = save_snapshot(frame)
        logger.info("Person detected (no face visible) — sending alert")
        await send_telegram_photo(
            snapshot_path, "⚠️ Person detected in living room (face not visible)"
        )
        asyncio.create_task(escalate_unknown_vlm(frame.copy(), snapshot_path))
        await ingest_event(
            "Person detected in living room (face not visible)",
            {
                "detection_type": "person",
                "identity": "unknown",
                "known": False,
                "snapshot_path": snapshot_path,
                "camera_id": "living-room",
            },
        )

    # Departure tracking
    departed = []
    now = datetime.now()
    for identity, info in list(people_present.items()):
        if identity.startswith("_"):
            continue
        last_seen = datetime.fromisoformat(info["last_seen"])
        if (now - last_seen).total_seconds() > PERSON_GONE_TIMEOUT:
            departed.append(identity)

    for identity in departed:
        info = people_present.pop(identity)
        if not identity.startswith("unknown_"):
            logger.info("Person departed: %s", identity)
            await ingest_event(
                f"{identity} left the living room",
                {
                    "detection_type": "departure",
                    "identity": identity,
                    "camera_id": "living-room",
                    "arrived_at": info.get("arrived_at"),
                    "departed_at": now_str,
                },
            )


async def process_frame_armed(frame: np.ndarray):
    """Armed mode: motion + YOLO + face identification + escalation."""
    global last_detection_time, last_detection_details, _last_motion_time, _last_motion_score

    now = datetime.now()
    now_str = now.isoformat()

    # Motion gate — skip heavy processing if nothing moved
    motion_detected, motion_score = detect_motion(frame)
    _last_motion_score = motion_score

    if not motion_detected and not people_present:
        return

    # YOLO detection
    detections = detect_objects(frame)
    persons = [d for d in detections if d["class"] == "person"]

    # Log general motion (non-person) with cooldown
    if motion_detected and not persons:
        now_ts = time.time()
        if now_ts - _last_motion_time >= MOTION_COOLDOWN:
            _last_motion_time = now_ts
            object_classes = list({d["class"] for d in detections})
            snapshot_path = save_snapshot(frame)
            logger.info(
                "Motion detected (score=%.3f, objects=%s)", motion_score, object_classes or "none"
            )
            await ingest_event(
                f"Motion detected in living room (score={motion_score})"
                + (f", objects: {', '.join(object_classes)}" if object_classes else ""),
                {
                    "detection_type": "motion",
                    "motion_score": motion_score,
                    "objects": object_classes,
                    "snapshot_path": snapshot_path,
                    "camera_id": "living-room",
                },
            )

    if not persons and not people_present:
        return

    # Person tracking
    seen_this_frame = set()

    if persons:
        last_detection_time = now_str
        last_detection_details = {"persons": len(persons), "total_objects": len(detections)}

        faces = detect_faces(frame)

        for face in faces:
            name, sim = match_face(face["embedding"])
            if name:
                seen_this_frame.add(name)
                if name not in people_present:
                    snapshot_path = save_snapshot(frame)
                    people_present[name] = {
                        "last_seen": now_str,
                        "arrived_at": now_str,
                        "snapshot": snapshot_path,
                    }
                    logger.info("Person appeared: %s (sim=%.3f)", name, sim)
                    await ingest_event(
                        f"{name} detected in living room",
                        {
                            "detection_type": "person",
                            "identity": name,
                            "known": True,
                            "snapshot_path": snapshot_path,
                            "camera_id": "living-room",
                            "similarity": round(sim, 3),
                        },
                    )
                else:
                    people_present[name]["last_seen"] = now_str
            else:
                unknown_id = get_unknown_id()
                snapshot_path = save_snapshot(frame)
                people_present[unknown_id] = {
                    "last_seen": now_str,
                    "arrived_at": now_str,
                    "snapshot": snapshot_path,
                    "unknown": True,
                }
                seen_this_frame.add(unknown_id)
                logger.info("UNKNOWN person detected (id=%s) — sending alert", unknown_id)
                await send_telegram_photo(snapshot_path, "⚠️ Unknown person detected in living room")
                asyncio.create_task(escalate_unknown_vlm(frame.copy(), snapshot_path))

        if persons and not faces:
            generic_key = "_person_no_face"
            if generic_key not in people_present:
                snapshot_path = save_snapshot(frame)
                people_present[generic_key] = {
                    "last_seen": now_str,
                    "arrived_at": now_str,
                    "snapshot": snapshot_path,
                }
                seen_this_frame.add(generic_key)
                logger.info("Person detected (no face visible) — sending alert")
                await send_telegram_photo(
                    snapshot_path, "⚠️ Person detected in living room (face not visible)"
                )
                asyncio.create_task(escalate_unknown_vlm(frame.copy(), snapshot_path))
                await ingest_event(
                    "Person detected in living room (face not visible)",
                    {
                        "detection_type": "person",
                        "identity": "unknown",
                        "known": False,
                        "snapshot_path": snapshot_path,
                        "camera_id": "living-room",
                    },
                )
            else:
                people_present[generic_key]["last_seen"] = now_str
                seen_this_frame.add(generic_key)

    # Departure tracking
    departed = []
    for identity, info in list(people_present.items()):
        if identity not in seen_this_frame:
            last_seen = datetime.fromisoformat(info["last_seen"])
            if (now - last_seen).total_seconds() > PERSON_GONE_TIMEOUT:
                departed.append(identity)

    for identity in departed:
        info = people_present.pop(identity)
        if not identity.startswith("_") and not identity.startswith("unknown_"):
            logger.info("Person departed: %s", identity)
            await ingest_event(
                f"{identity} left the living room",
                {
                    "detection_type": "departure",
                    "identity": identity,
                    "camera_id": "living-room",
                    "arrived_at": info.get("arrived_at"),
                    "departed_at": now_str,
                },
            )


# ============== Enrollment ==============


async def enroll_person(name: str, num_frames: int = 5) -> dict:
    """Capture multiple frames and enroll a person's face."""
    ensure_models_loaded()
    camera = CameraStream(RTSP_URL)
    embeddings = []

    for i in range(num_frames * 3):
        frame = camera.read()
        if frame is None:
            continue

        faces = detect_faces(frame)
        if faces:
            best_face = max(faces, key=lambda f: f["det_score"])
            embeddings.append(best_face["embedding"])
            logger.info("Enrollment frame %d/%d captured for %s", len(embeddings), num_frames, name)
            if len(embeddings) >= num_frames:
                break

        await asyncio.sleep(0.5)

    camera.release()

    if len(embeddings) < 2:
        return {
            "success": False,
            "error": f"Could only capture {len(embeddings)} face(s), need at least 2",
        }

    avg_embedding = np.mean(embeddings, axis=0)
    avg_embedding = avg_embedding / np.linalg.norm(avg_embedding)

    enrolled_faces[name] = avg_embedding
    save_enrolled_faces()

    snapshot_path = None
    cam = CameraStream(RTSP_URL)
    frame = cam.read()
    cam.release()
    if frame is not None:
        snapshot_path = save_snapshot(frame)

    logger.info("Enrolled %s with %d face samples", name, len(embeddings))
    return {
        "success": True,
        "name": name,
        "samples": len(embeddings),
        "snapshot_path": snapshot_path,
    }


# ============== VLM Analysis (Ollama) ==============


async def analyze_image_vlm(
    frame: np.ndarray, prompt: str = "Describe what you see in this image in detail."
) -> str:
    """Send a frame to llama3.2-vision via Ollama for VLM analysis."""
    _, buf = cv2.imencode(".jpg", frame)
    img_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

    payload = {
        "model": "llama3.2-vision:11b",
        "messages": [
            {
                "role": "system",
                "content": "You are Robothor's vision system. Describe what you see clearly and concisely. Note any people, objects, and notable details.",
            },
            {"role": "user", "content": prompt, "images": [img_b64]},
        ],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024, "num_gpu": 999},
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json()["message"]["content"]


# ============== HTTP Server ==============


def make_json_response(status: str, body_dict: dict) -> bytes:
    """Build a minimal HTTP response."""
    body = json.dumps(body_dict)
    return f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode()


async def handle_request(reader, writer):
    """Handle HTTP requests on the service endpoint."""
    data = await reader.read(8192)
    request = data.decode(errors="replace")

    first_line = request.split("\n")[0] if request else ""
    parts = first_line.split()
    method = parts[0] if len(parts) >= 1 else ""
    path = parts[1] if len(parts) >= 2 else ""

    # Parse body for POST
    req_body = ""
    body_start = request.find("\r\n\r\n")
    if body_start >= 0:
        req_body = request[body_start + 4 :]

    try:
        if path == "/health":
            resp = make_json_response(
                "200 OK",
                {
                    "running": running,
                    "mode": current_mode,
                    "started_at": _start_time,
                    "people_present": [k for k in people_present if not k.startswith("_")],
                    "last_detection": last_detection_time,
                    "last_detection_details": last_detection_details,
                    "last_motion_score": _last_motion_score,
                    "motion_threshold": MOTION_THRESHOLD,
                    "enrolled_faces": list(enrolled_faces.keys()),
                    "models_loaded": _models_loaded,
                    "camera_url": RTSP_URL,
                },
            )

        elif path == "/mode" and method == "GET":
            resp = make_json_response(
                "200 OK", {"mode": current_mode, "valid_modes": list(VALID_MODES)}
            )

        elif path == "/mode" and method == "POST":
            try:
                req_data = json.loads(req_body)
                new_mode = set_mode(req_data["mode"])
                resp = make_json_response(
                    "200 OK", {"mode": new_mode, "message": f"Switched to {new_mode}"}
                )
            except (json.JSONDecodeError, KeyError):
                resp = make_json_response(
                    "400 Bad Request", {"error": 'Send JSON: {"mode": "basic|armed|disarmed"}'}
                )
            except ValueError as e:
                resp = make_json_response("400 Bad Request", {"error": str(e)})

        elif path == "/detections":
            ensure_models_loaded()
            camera = CameraStream(RTSP_URL)
            frame = camera.read()
            camera.release()
            if frame is not None:
                detections = detect_objects(frame)
                snapshot_path = save_snapshot(frame)
                resp = make_json_response(
                    "200 OK",
                    {
                        "detections": detections,
                        "snapshot_path": snapshot_path,
                        "source": "vision_service",
                    },
                )
            else:
                resp = make_json_response(
                    "503 Service Unavailable", {"detections": [], "error": "Camera unavailable"}
                )

        elif path == "/identifications":
            ensure_models_loaded()
            camera = CameraStream(RTSP_URL)
            frame = camera.read()
            camera.release()
            if frame is not None:
                faces = detect_faces(frame)
                results = []
                for face in faces:
                    name, sim = match_face(face["embedding"])
                    results.append(
                        {
                            "name": name or "unknown",
                            "known": name is not None,
                            "similarity": round(sim, 3),
                            "det_score": round(face["det_score"], 3),
                        }
                    )
                resp = make_json_response(
                    "200 OK",
                    {
                        "identifications": results,
                        "people_present": [k for k in people_present if not k.startswith("_")],
                    },
                )
            else:
                resp = make_json_response(
                    "503 Service Unavailable",
                    {"identifications": [], "error": "Camera unavailable"},
                )

        elif path == "/enroll" and method == "POST":
            try:
                req_data = json.loads(req_body)
                name = req_data.get("name", "")
                if not name:
                    resp = make_json_response("400 Bad Request", {"error": "Name is required"})
                else:
                    result = await enroll_person(name)
                    status = "200 OK" if result.get("success") else "422 Unprocessable Entity"
                    resp = make_json_response(status, result)
            except json.JSONDecodeError:
                resp = make_json_response("400 Bad Request", {"error": "Invalid JSON"})

        elif path == "/look" and method == "POST":
            # VLM scene analysis — works in any mode
            try:
                req_data = json.loads(req_body) if req_body.strip() else {}
            except json.JSONDecodeError:
                req_data = {}
            prompt = req_data.get("prompt", "Describe what you see in this image in detail.")
            camera = CameraStream(RTSP_URL)
            frame = camera.read()
            camera.release()
            if frame is not None:
                snapshot_path = save_snapshot(frame)
                try:
                    description = await analyze_image_vlm(frame, prompt)
                    resp = make_json_response(
                        "200 OK",
                        {
                            "description": description,
                            "snapshot_path": snapshot_path,
                            "prompt": prompt,
                        },
                    )
                except Exception as e:
                    resp = make_json_response(
                        "502 Bad Gateway",
                        {"error": f"VLM analysis failed: {e}", "snapshot_path": snapshot_path},
                    )
            else:
                resp = make_json_response(
                    "503 Service Unavailable", {"error": "Camera unavailable"}
                )

        else:
            resp = make_json_response(
                "404 Not Found",
                {
                    "error": "Not found",
                    "endpoints": [
                        "GET /health",
                        "GET /mode",
                        "POST /mode",
                        "GET /detections",
                        "GET /identifications",
                        "POST /enroll",
                        "POST /look",
                    ],
                },
            )

    except Exception as e:
        logger.error("Request handler error: %s", e, exc_info=True)
        resp = make_json_response("500 Internal Server Error", {"error": str(e)})

    writer.write(resp)
    await writer.drain()
    writer.close()


# ============== Main Loop ==============


async def detection_loop():
    """Main loop — dispatches to the appropriate handler based on current mode."""
    global running

    camera = CameraStream(RTSP_URL)
    interval = 1.0 / CAPTURE_FPS
    reconnect_wait = 5.0

    logger.info("Detection loop starting (%.1f FPS, mode=%s)", CAPTURE_FPS, current_mode)

    while running:
        t0 = time.time()

        if current_mode == "disarmed":
            # Do nothing, just keep the camera warm and check periodically
            await asyncio.sleep(interval)
            continue

        frame = camera.read()
        if frame is None:
            logger.warning("No frame, waiting %.0fs to reconnect...", reconnect_wait)
            await asyncio.sleep(reconnect_wait)
            camera = CameraStream(RTSP_URL)
            continue

        try:
            if current_mode == "basic":
                await process_frame_basic(frame)
            elif current_mode == "armed":
                await process_frame_armed(frame)
        except Exception as e:
            logger.error("Frame processing error: %s", e, exc_info=True)

        elapsed = time.time() - t0
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    camera.release()
    logger.info("Detection loop stopped")


async def main():
    """Start the vision service."""
    global running, _start_time, current_mode

    def handle_signal(sig, _frame):
        global running
        logger.info("Signal %s received, shutting down...", sig)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    _start_time = datetime.now().isoformat()

    # Load persisted mode
    current_mode = load_mode()
    logger.info("Robothor Vision Service starting (mode=%s)...", current_mode)

    # Load enrolled faces (always — they're just a JSON file)
    load_enrolled_faces()

    # Always load YOLO + InsightFace — 306MB total, negligible on 128GB
    ensure_models_loaded()

    # Ensure directories
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    FACE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Start HTTP server
    server = await asyncio.start_server(handle_request, "127.0.0.1", HEALTH_PORT)
    logger.info("HTTP endpoint listening on port %d", HEALTH_PORT)

    # Run detection loop
    try:
        await detection_loop()
    finally:
        server.close()
        await server.wait_closed()
        logger.info("Vision service stopped")


if __name__ == "__main__":
    asyncio.run(main())
