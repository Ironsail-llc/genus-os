# Robothor Vision System
**Version:** 2.0
**Last Updated:** 2026-02-16
**Hardware:** USB webcam at /dev/video0 → MediaMTX RTSP → Vision Service

---

## Overview

One always-on service with event-triggered smart detection. Models (YOLO + InsightFace) are **always loaded** at startup (~306MB — negligible on 128GB).

| Mode | What runs | CPU cost | Use case |
|------|-----------|----------|----------|
| **disarmed** | Nothing — camera connected, health up | ~0 | Don't want any monitoring |
| **basic** | Motion → YOLO → InsightFace → instant Telegram alert + async VLM | Low (1 FPS) | Default — always on |
| **armed** | Same as basic + frame-level person tracking | Low (1 FPS) | Manual override / debug |

The service starts on boot and runs forever. You switch modes at runtime — no restart needed. Mode persists across restarts.

**Key design:** The image IS the escalation. Philip gets the snapshot within 2 seconds. AI analysis arrives 15-30 seconds later as a follow-up.

```bash
# Switch modes
curl -X POST http://localhost:8600/mode -d '{"mode":"armed"}'
curl -X POST http://localhost:8600/mode -d '{"mode":"basic"}'
curl -X POST http://localhost:8600/mode -d '{"mode":"disarmed"}'

# Or via orchestrator
curl -X POST http://localhost:9099/vision/mode -H "Content-Type: application/json" -d '{"mode":"armed"}'

# Or via MCP tool
set_vision_mode("armed")
```

On-demand endpoints (`/vision/look`, `/detections`, `/identifications`, `/enroll`) always work regardless of mode — they capture a fresh frame on request.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  VISION SERVICE (always-on, mode-switchable)                     │
│  systemd: robothor-vision.service | port 8600                   │
│  Models loaded at startup: YOLO (6MB) + InsightFace (300MB)     │
│                                                                 │
│  ┌───────────────────────────────────────────────────────┐      │
│  │  DISARMED: Sleep loop. Health endpoint only.          │      │
│  ├───────────────────────────────────────────────────────┤      │
│  │  BASIC (default): Smart detection pipeline (1 FPS)    │      │
│  │    Motion → YOLO (~50ms) → person detected?           │      │
│  │      ├─ Person → InsightFace (~200ms)                 │      │
│  │      │   ├─ Known → log arrival, NO alert             │      │
│  │      │   └─ Unknown → INSTANT Telegram photo (<2s)    │      │
│  │      │       └─ async: VLM description → Telegram text│      │
│  │      ├─ Person (no face) → INSTANT Telegram photo     │      │
│  │      └─ Non-person motion → log with cooldown         │      │
│  │    120s cooldown between person alerts                 │      │
│  ├───────────────────────────────────────────────────────┤      │
│  │  ARMED: Same pipeline + per-frame person tracking     │      │
│  │    (departure tracking, seen_this_frame, etc.)        │      │
│  └───────────────────────────────────────────────────────┘      │
│                                                                 │
│  Telegram alerts: snapshot sent via Bot API (sendPhoto)         │
│  VLM follow-up: fire-and-forget llama3.2-vision → sendMessage  │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼ events (on state changes only)
┌─────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR (port 9099) + VLM (on-demand)                     │
│                                                                 │
│  POST /ingest (camera channel)                                  │
│  → fact_extraction.py → memory_facts + memory_entities          │
│                                                                 │
│  POST /vision/look (on-demand, any mode)                        │
│  → snapshot → llama3.2-vision → rich scene description          │
│                                                                 │
│  POST /vision/mode (switch modes via orchestrator)              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

| Component | Model/Tool | Size | Purpose |
|-----------|-----------|------|---------|
| Object Detection | YOLOv8-nano | ~6 MB | Person/object detection at 1 FPS |
| Face Recognition | InsightFace buffalo_l (ArcFace) | ~300 MB | Face detection + embedding |
| Vision LLM | llama3.2-vision:11b | 7.8 GB | Scene analysis (on-demand) |
| Camera | USB webcam → MediaMTX RTSP | — | 640x480 @ 30fps H.264 |
| Inference | ONNX Runtime (CPU/CUDA) | — | InsightFace backend |

---

## Detection Flow

```
Frame captured at 1 FPS
    │
    ▼
Motion detected? (frame diff, threshold 0.15)
    │
    ├─ No motion → skip (no compute)
    │
    └─ Motion detected
        │
        ▼
    YOLOv8-nano detection (~50ms, conf > 0.5)
        │
        ├─ No persons → log motion with 30s cooldown
        │
        └─ Person(s) detected
            │
            ▼
        InsightFace face detection + embedding (~200ms)
            │
            ├─ Face found → cosine match vs enrolled faces
            │   │
            │   ├─ Match (sim > 0.45) → KNOWN (e.g. "Philip")
            │   │   └─ Log arrival, NO Telegram alert
            │   │
            │   └─ No match → UNKNOWN PERSON
            │       │ (120s alert cooldown)
            │       ├─ IMMEDIATE: save snapshot → Telegram photo (<2s)
            │       └─ ASYNC: llama3.2-vision → Telegram text (15-30s)
            │
            └─ No face visible → PERSON (can't ID)
                │ (120s alert cooldown)
                ├─ IMMEDIATE: save snapshot → Telegram photo (<2s)
                └─ ASYNC: llama3.2-vision → Telegram text (15-30s)
```

---

## Escalation Logic

When an **unknown person** is detected (or person with no visible face):
1. Snapshot saved to `memory/snapshots/YYYY-MM-DD/HHMMSS.jpg`
2. **IMMEDIATE:** Snapshot sent to Philip via Telegram Bot API (`sendPhoto`) — under 2 seconds
3. **ASYNC (fire-and-forget):** VLM analysis via llama3.2-vision → follow-up Telegram text message with description (15-30s later)
4. High-importance fact ingested via `/ingest` (importance_score: 0.8)
5. 120-second cooldown between person alerts (prevents spam from sustained motion)

When a **known person** appears or departs:
1. Snapshot saved
2. Normal-importance fact ingested via `/ingest`
3. **No Telegram alert** — known people don't trigger notifications
4. Available in memory search (`search_memory("who was here today")`)

---

## API Reference

### Orchestrator Endpoints (port 9099)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/vision/look` | Capture snapshot → VLM analysis → return description |
| POST | `/vision/detect` | Capture snapshot → YOLO detections → return object list |
| POST | `/vision/identify` | Capture snapshot → face detection → match against entities |
| GET | `/vision/status` | Vision service state: running?, last_detection, people_present |
| POST | `/vision/enroll` | Capture face → name → store embedding |
| GET | `/vision/mode` | Get current vision mode |
| POST | `/vision/mode` | Switch vision mode (disarmed/basic/armed) |

#### POST /vision/look

```bash
curl -X POST http://localhost:9099/vision/look \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What do you see?"}'
```

Response:
```json
{
  "description": "I can see the living room with the reef tank...",
  "snapshot_path": "memory/snapshots/2026-02-07/143201.jpg",
  "prompt": "What do you see?"
}
```

#### POST /vision/detect

```bash
curl -X POST http://localhost:9099/vision/detect
```

Response:
```json
{
  "detections": [
    {"class": "person", "confidence": 0.92, "bbox": [100.0, 50.0, 300.0, 400.0]},
    {"class": "couch", "confidence": 0.87, "bbox": [0.0, 200.0, 640.0, 480.0]}
  ],
  "source": "vision_service"
}
```

#### POST /vision/identify

```bash
curl -X POST http://localhost:9099/vision/identify
```

Response:
```json
{
  "identifications": [
    {"name": "Philip", "known": true, "similarity": 0.72, "det_score": 0.95}
  ],
  "people_present": ["Philip"]
}
```

#### GET /vision/status

```bash
curl http://localhost:9099/vision/status
```

Response:
```json
{
  "running": true,
  "people_present": ["Philip"],
  "last_detection": "2026-02-07T14:32:01",
  "enrolled_faces": ["Philip"],
  "camera_url": "rtsp://localhost:8554/webcam"
}
```

#### POST /vision/enroll

```bash
curl -X POST http://localhost:9099/vision/enroll \
  -H "Content-Type: application/json" \
  -d '{"name": "Philip"}'
```

Response:
```json
{
  "success": true,
  "name": "Philip",
  "samples": 5,
  "snapshot_path": "memory/snapshots/2026-02-07/143201.jpg"
}
```

### Vision Service Endpoints (port 8600)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Service health + mode + state |
| GET | `/mode` | Current mode |
| POST | `/mode` | Switch mode: `{"mode": "basic\|armed\|disarmed"}` |
| GET | `/detections` | Fresh YOLO detection (loads models on demand) |
| GET | `/identifications` | Fresh face identification (loads models on demand) |
| POST | `/enroll` | Face enrollment (loads models on demand) |

---

## MCP Tools

| Tool | Input | Output |
|------|-------|--------|
| `look` | optional: `prompt` (what to look for) | VLM scene description |
| `who_is_here` | none | List of detected/identified people + current mode |
| `enroll_face` | `name` (required) | Confirmation of face enrollment |
| `set_vision_mode` | `mode` (disarmed/basic/armed) | Confirmation of mode switch |

Usage from Claude Code / MCP client:
```
look("What's happening in the living room?")
who_is_here()
enroll_face("Philip")
set_vision_mode("armed")    # leaving the house
set_vision_mode("basic")    # back home
set_vision_mode("disarmed") # don't want any monitoring
```

---

## Face Enrollment Workflow

1. Person stands in front of camera
2. Agent calls `enroll_face("Name")` or `POST /vision/enroll {"name": "Name"}`
3. Vision service captures 5+ frames with face visible
4. Face embeddings extracted via InsightFace ArcFace
5. Embeddings averaged and L2-normalized
6. Stored in `memory/faces/enrolled_faces.json`
7. Future detections match against enrolled faces (cosine similarity > 0.45)

---

## Snapshot Storage

```
/home/philip/robothor/brain/memory/
├── snapshots/              # Date-organized detection snapshots
│   ├── 2026-02-07/
│   │   ├── 143201.jpg
│   │   ├── 143215.jpg
│   │   └── ...
│   └── 2026-02-08/
│       └── ...
└── faces/                  # Enrolled face embeddings
    └── enrolled_faces.json
```

Cleanup: Snapshots older than 30 days are deleted daily at 4:00 AM (cron).

---

## Memory Integration

Vision events use the existing memory pipeline:

**Source channel:** `camera`
**Content type:** `event`

Example ingested facts:
- "Philip detected in living room" (known person arrival)
- "Philip left the living room" (departure)
- "Unknown person detected in living room. Male, ~30s, wearing blue shirt..." (unknown + VLM)

These are searchable via `search_memory("who was here today")` and appear in the entity graph.

---

## Configuration

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| Default mode | basic | `VISION_DEFAULT_MODE` | Mode on first boot (before any persisted mode) |
| Capture FPS | 1.0 | `CAPTURE_FPS` | Frames per second to process |
| YOLO confidence | 0.5 | `YOLO_CONFIDENCE` | Minimum detection confidence |
| Face match threshold | 0.45 | `FACE_MATCH_THRESHOLD` | Cosine similarity for face match |
| Person gone timeout | 60s | `PERSON_GONE_TIMEOUT` | Seconds before marking departed |
| Motion cooldown | 30s | `MOTION_COOLDOWN` | Seconds between non-person motion logs |
| Person alert cooldown | 120s | `PERSON_ALERT_COOLDOWN` | Seconds between unknown person Telegram alerts |
| Health port | 8600 | `VISION_HEALTH_PORT` | HTTP health endpoint port |
| RTSP URL | rtsp://localhost:8554/webcam | `RTSP_URL` | Camera stream URL |
| Snapshot dir | memory/snapshots | `SNAPSHOT_DIR` | Where to save snapshots |

---

## Memory Budget Impact

Models are loaded unconditionally at startup — always hot and ready.

| Component | Size | Notes |
|-----------|------|-------|
| YOLOv8-nano | ~6 MB | Loaded at startup, always resident |
| InsightFace buffalo_l | ~300 MB | Loaded at startup, always resident |
| llama3.2-vision:11b | 7.8 GB | Loaded on-demand for VLM analysis |
| OpenCV + numpy | ~50 MB RAM | Frame processing |
| **Vision total** | **~350 MB** | **Negligible vs 128GB budget** |

---

## Remote Access

**Live stream:** `https://cam.robothor.ai/webcam/` (HLS via Cloudflare tunnel)

Protected by **Cloudflare Access** (Zero Trust):
- Auth: email one-time PIN
- Allowed: `philip@ironsail.ai`, `robothor@ironsail.ai`
- Session: 24 hours
- All other visitors blocked at Cloudflare edge

All camera ports are bound to `127.0.0.1` — no direct access from the network. The only external path is through the Cloudflare tunnel with Access authentication.

| Port | Protocol | Binding | Purpose |
|------|----------|---------|---------|
| 8554 | RTSP | 127.0.0.1 | Camera stream (MediaMTX) |
| 8889 | WebRTC | 127.0.0.1 | WebRTC (unused) |
| 8890 | HLS | 127.0.0.1 | HTTP Live Streaming → tunnel |
| 8600 | HTTP | 127.0.0.1 | Vision service health/API |

---

## Service Management

```bash
# Start
sudo systemctl start robothor-vision

# Stop
sudo systemctl stop robothor-vision

# Status
sudo systemctl status robothor-vision

# Logs
journalctl -u robothor-vision -f

# Restart
sudo systemctl restart robothor-vision
```

---

## Troubleshooting

**Camera not connecting:**
- Check MediaMTX is running: `systemctl status mediamtx`
- Test RTSP: `ffmpeg -rtsp_transport tcp -i rtsp://localhost:8554/webcam -frames:v 1 -y /tmp/test.jpg`
- Check webcam device: `ls /dev/video0`

**YOLO model download:**
- First run downloads `yolov8n.pt` (~6MB) automatically
- Downloaded to working directory or `~/.cache/`

**InsightFace model download:**
- First run downloads `buffalo_l` model pack (~300MB)
- Downloaded to `~/.insightface/models/`
- If download fails: `python -c "from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_l')"`

**Face recognition not matching:**
- Re-enroll with better lighting
- Ensure face is clearly visible (not side-profile)
- Lower `FACE_MATCH_THRESHOLD` (default 0.45, try 0.40)
- Check enrolled faces: `cat memory/faces/enrolled_faces.json | python -m json.tool | head`

**CUDA/GPU issues:**
- InsightFace falls back to CPU if CUDA unavailable
- YOLO uses PyTorch CUDA if available
- Check: `python -c "import torch; print(torch.cuda.is_available())"`
