# Configuration

All configuration is via environment variables with sensible defaults. No config files required for basic usage. See `infra/robothor.env.example` for a complete template.

## Loading

```python
from robothor.config import get_config

cfg = get_config()
print(cfg.db.dsn)           # "dbname=robothor_memory host=127.0.0.1 ..."
print(cfg.redis.url)        # "redis://127.0.0.1:6379/0"
print(cfg.ollama.base_url)  # "http://127.0.0.1:11434"
print(cfg.workspace)        # Path("/home/your-user/robothor")
```

The config is a singleton. Call `reset_config()` in tests to reload from environment.

## Database (PostgreSQL + pgvector)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_DB_HOST` | `127.0.0.1` | PostgreSQL host |
| `ROBOTHOR_DB_PORT` | `5432` | PostgreSQL port |
| `ROBOTHOR_DB_NAME` | `robothor_memory` | Database name |
| `ROBOTHOR_DB_USER` | `$USER` | Database user (falls back to system user) |
| `ROBOTHOR_DB_PASSWORD` | *(empty)* | Database password |

## Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_REDIS_HOST` | `127.0.0.1` | Redis host |
| `ROBOTHOR_REDIS_PORT` | `6379` | Redis port |
| `ROBOTHOR_REDIS_DB` | `0` | Redis database number |
| `ROBOTHOR_REDIS_PASSWORD` | *(empty)* | Redis password |
| `ROBOTHOR_REDIS_MAXMEMORY` | `2gb` | Redis maxmemory (Docker only) |
| `REDIS_URL` | *(derived)* | Full Redis URL override (e.g., `redis://:pass@host:6379/0`) |

## Ollama (LLM Inference)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_OLLAMA_HOST` | `127.0.0.1` | Ollama server host |
| `ROBOTHOR_OLLAMA_PORT` | `11434` | Ollama server port |
| `ROBOTHOR_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Embedding model (1024-dim) |
| `ROBOTHOR_RERANKER_MODEL` | `Qwen3-Reranker-0.6B:F16` | Cross-encoder reranker |
| `ROBOTHOR_GENERATION_MODEL` | `qwen3:8b` | RAG generation model |
| `ROBOTHOR_VISION_MODEL` | `llama3.2-vision:11b` | Vision scene analysis model |
| `ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB` | `24` | autoDream only unloads the generation model when available memory (GiB) drops below this; `0` disables the unload entirely |

## Memory Generation Provider

Memory generation (fact extraction, episode summaries, insight discovery,
conflict classification, intent inference) can be offloaded from the local
Ollama GPU to a remote provider. Embeddings and reranking always stay local.
On remote failure the system falls back to local Ollama with a WARNING log
containing `MEMORY_GENERATION_REMOTE_FALLBACK` — watch for that marker.

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_MEMORY_GENERATION_PROVIDER` | `ollama` | `ollama` (local, unchanged behavior) or `openrouter` (remote) |
| `ROBOTHOR_MEMORY_GENERATION_REMOTE_MODEL` | `openrouter/xiaomi/mimo-v2.5` | Remote model when the provider is `openrouter` |
| `ROBOTHOR_MEMORY_GENERATION_MIN_INTERVAL_S` | `1.5` | Minimum seconds between remote generation calls (`0` disables pacing) |

`openrouter` requires `OPENROUTER_API_KEY` in the environment; if it is
missing, an ERROR is logged once and generation stays local. Remote 429/503
responses are retried up to 3 attempts with jittered exponential backoff
(finite `Retry-After` honored, each sleep capped at 20s, 45s total budget);
timeouts and network errors get one retry; other errors fall back to local
immediately.

## Service Ports

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_API_PORT` | `9099` | RAG Orchestrator / API server |
| `ROBOTHOR_BRIDGE_PORT` | `9100` | Bridge service (CRM, contacts) |
| `ROBOTHOR_VISION_PORT` | `8600` | Vision service |
| `ROBOTHOR_HELM_PORT` | `3004` | Helm dashboard |
| `ROBOTHOR_TTS_PORT` | `8880` | TTS service |

## Vision

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_VISION_MODE` | `disarmed` | Default mode: `disarmed`, `basic`, `armed`, `disabled` |
| `ROBOTHOR_CAMERA_SOURCE` | `/dev/video0` | Camera device, RTSP URL, or video file |
| `ROBOTHOR_CAMERA_WIDTH` | `640` | Capture width |
| `ROBOTHOR_CAMERA_HEIGHT` | `480` | Capture height |
| `ROBOTHOR_YOLO_MODEL` | `yolov8n` | YOLO variant: `yolov8n` (6MB), `yolov8s` (22MB), `yolov8m` (52MB) |
| `ROBOTHOR_VLM_MODEL` | `llama3.2-vision:11b` | Vision language model for scene analysis |

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_MEMORY_TTL_HOURS` | `48` | Short-term memory TTL |
| `ROBOTHOR_IMPORTANCE_THRESHOLD` | `0.3` | Minimum importance for long-term archival |
| `ROBOTHOR_MEMORY_BLOCK_MAX_CHARS` | `5000` | Maximum characters per memory block |
| `ROBOTHOR_MEMORY_DIR` | `$WORKSPACE/memory` | Memory file directory |

## Workspace

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_WORKSPACE` | `~/robothor` | Base directory for runtime data |
| `ROBOTHOR_LOG_DIR` | `/var/log/robothor` | Log directory |

## Event Bus

| Variable | Default | Description |
|----------|---------|-------------|
| `EVENT_BUS_ENABLED` | `true` | Enable Redis Streams event bus |
| `EVENT_BUS_MAXLEN` | `10000` | Max entries per stream (circular buffer) |
| `ROBOTHOR_CAPABILITIES_MANIFEST` | `$WORKSPACE/agent_capabilities.json` | Agent RBAC manifest path |
| `ROBOTHOR_SERVICES_MANIFEST` | `$WORKSPACE/robothor-services.json` | Service registry path |

## Notifications (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_TELEGRAM_BOT_TOKEN` | *(empty)* | Telegram bot token for alerts |
| `ROBOTHOR_TELEGRAM_CHAT_ID` | *(empty)* | Telegram chat ID for notifications |

## Failure-mode detectors

See [Observability](OBSERVABILITY.md#failure-mode-detectors) for what each
detector watches.

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTHOR_DETECTORS_ENABLED` | `1` | `0` disables every detector |
| `ROBOTHOR_DECLARED_TOOL_OUTAGES` | *(empty)* | `tool:reason,tool:reason` — tool outages the operator has already decided about, so `tool_outage_detector` stops alerting about them |

## Service URL Overrides

The service registry supports environment variable overrides for any service:

| Variable | Overrides |
|----------|-----------|
| `BRIDGE_URL` | Bridge base URL |
| `ORCHESTRATOR_URL` | API server base URL |
| `VISION_URL` | Vision service base URL |
| `OLLAMA_URL` | Ollama base URL |
| `SEARXNG_URL` | SearXNG search URL |

These take precedence over `robothor-services.json` values.
