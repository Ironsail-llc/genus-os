# Vision Sentry Example

Use Robothor's vision system as an intelligent security camera. Detects motion, identifies objects with YOLO, recognizes faces with InsightFace, and sends alerts for unknown persons via configurable webhooks.

## Prerequisites

1. **A camera** -- USB webcam, IP camera with RTSP, or any OpenCV-compatible video source.
2. **Ollama** (optional) -- for AI scene analysis of detected events:
   ```bash
   ollama pull llama3.2-vision:11b
   ```
3. **Python dependencies**:
   ```bash
   pip install robothor[vision]
   ```

This installs YOLO (ultralytics), InsightFace, and ONNX Runtime. Models are downloaded automatically on first use (~6MB for YOLOv8-nano, ~300MB for InsightFace buffalo_l).

## Configure

```bash
# Camera source (RTSP URL, device index, or video file)
export RTSP_URL=rtsp://localhost:8554/webcam

# Vision service port for health checks and API
export VISION_HEALTH_PORT=8600

# Detection tuning
export MOTION_THRESHOLD=0.15         # Fraction of changed pixels to trigger
export PERSON_ALERT_COOLDOWN=120     # Seconds between alerts for same person
export CAPTURE_FPS=1.0               # Frames per second to process

# Optional: Ollama for VLM scene analysis
export OLLAMA_URL=http://localhost:11434
export ROBOTHOR_VISION_MODEL=llama3.2-vision:11b
```

## Run

```bash
python main.py
```

The service starts in `basic` mode by default:
- Watches for motion via frame differencing
- On motion, runs YOLO object detection
- If a person is detected, runs face recognition
- Unknown persons trigger webhook alerts with snapshot images
- Known persons are logged silently

## Modes

| Mode | Behavior |
|------|----------|
| `disarmed` | Camera connected, health endpoint up, no processing |
| `basic` | Motion-gated detection: motion triggers YOLO + face ID + alerts |
| `armed` | Full detection on every frame, per-frame person tracking |

Switch modes at runtime:
```bash
curl -X POST http://localhost:8600/mode -d '{"mode": "armed"}'
```

## API Endpoints

Once running, the service exposes these HTTP endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service status, people present, models loaded |
| GET | `/mode` | Current mode and valid modes |
| POST | `/mode` | Switch mode (body: `{"mode": "basic"}`) |
| GET | `/detections` | Run YOLO on current frame (on-demand) |
| GET | `/identifications` | Run face recognition on current frame |
| POST | `/enroll` | Enroll a person's face (body: `{"name": "Alice"}`) |
| POST | `/look` | VLM scene analysis (body: `{"prompt": "What do you see?"}`) |

## Alerts

The example configures a generic **webhook alert handler** that POSTs JSON to any URL you specify. You can also use the built-in Telegram handler or write your own by subclassing `AlertHandler`.

Webhook payload format:
```json
{
  "event_type": "unknown_person",
  "message": "Unknown person detected at camera-0",
  "metadata": {
    "snapshot_path": "/path/to/snapshot.jpg",
    "camera_id": "camera-0"
  }
}
```
