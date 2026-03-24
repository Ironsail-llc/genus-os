"""
Centralized configuration for Genus OS.

All configuration is loaded from environment variables with sensible defaults.
No hardcoded paths, no personal references.

Usage:
    from robothor.config import get_config
    cfg = get_config()
    print(cfg.db_name)       # "robothor_memory"
    print(cfg.workspace)     # "/home/user/robothor" or $ROBOTHOR_WORKSPACE
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL connection parameters."""

    host: str = ""  # empty = Unix socket (peer auth); set to 127.0.0.1 for TCP
    port: int = 5432
    name: str = "robothor_memory"
    user: str = "robothor"
    password: str = ""

    @property
    def dsn(self) -> str:
        """Return a psycopg2-compatible DSN string."""
        parts = [f"dbname={self.name}"]
        if self.host:
            parts.append(f"host={self.host}")
        parts.append(f"port={self.port}")
        if self.user:
            parts.append(f"user={self.user}")
        if self.password:
            parts.append(f"password={self.password}")
        return " ".join(parts)

    @property
    def dict(self) -> dict[str, str | int]:
        """Return a psycopg2.connect() kwargs dict."""
        d: dict[str, str | int] = {
            "dbname": self.name,
            "port": self.port,
        }
        if self.host:
            d["host"] = self.host
        if self.user:
            d["user"] = self.user
        if self.password:
            d["password"] = self.password
        return d


@dataclass(frozen=True)
class RedisConfig:
    """Redis connection parameters."""

    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


@dataclass(frozen=True)
class OllamaConfig:
    """Ollama LLM server parameters."""

    host: str = "127.0.0.1"
    port: int = 11434
    embedding_model: str = "qwen3-embedding:0.6b"
    reranker_model: str = "Qwen3-Reranker-0.6B:F16"
    generation_model: str = "nemotron-3-super"
    vision_model: str = "llama3.2-vision:11b"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class GarminConfig:
    """Garmin health sync configuration."""

    token_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "GARMIN_TOKEN_DIR",
                Path.home() / ".config" / "robothor" / "garmin_tokens",
            )
        )
    )


@dataclass(frozen=True)
class Config:
    """Top-level Genus OS configuration."""

    # Workspace
    workspace: Path = field(default_factory=lambda: Path.home() / "robothor")
    memory_dir: Path = field(default_factory=lambda: Path.home() / "robothor" / "memory")

    # Identity
    owner_name: str = "there"
    ai_name: str = "Robothor"

    # Components
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    garmin: GarminConfig = field(default_factory=GarminConfig)

    # Service ports (override via env or service registry)
    bridge_port: int = 9100
    orchestrator_port: int = 9099
    vision_port: int = 8600
    helm_port: int = 3004
    engine_port: int = 18800
    tts_port: int = 8880
    voice_port: int = 8765
    searxng_port: int = 8888

    # Desktop / Computer Use
    desktop_display: str = ":99"

    @property
    def bridge_url(self) -> str:
        return f"http://127.0.0.1:{self.bridge_port}"

    @property
    def orchestrator_url(self) -> str:
        return f"http://127.0.0.1:{self.orchestrator_port}"

    @property
    def vision_url(self) -> str:
        return f"http://127.0.0.1:{self.vision_port}"

    @property
    def voice_url(self) -> str:
        return f"http://127.0.0.1:{self.voice_port}"

    @property
    def searxng_url(self) -> str:
        return f"http://127.0.0.1:{self.searxng_port}"


# Singleton
_config: Config | None = None


def get_config() -> Config:
    """Get or create the singleton config from environment variables."""
    global _config
    if _config is not None:
        return _config
    _config = _load_from_env()
    return _config


def _load_from_env() -> Config:
    """Load configuration from environment variables."""
    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
    memory_dir = Path(os.environ.get("ROBOTHOR_MEMORY_DIR", workspace / "memory"))

    db = DatabaseConfig(
        host=os.environ.get("ROBOTHOR_DB_HOST", ""),
        port=int(os.environ.get("ROBOTHOR_DB_PORT", "5432")),
        name=os.environ.get("ROBOTHOR_DB_NAME", "robothor_memory"),
        user=os.environ.get("ROBOTHOR_DB_USER", os.environ.get("USER", "robothor")),
        password=os.environ.get("ROBOTHOR_DB_PASSWORD", ""),
    )

    redis_cfg = RedisConfig(
        host=os.environ.get("ROBOTHOR_REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("ROBOTHOR_REDIS_PORT", "6379")),
        db=int(os.environ.get("ROBOTHOR_REDIS_DB", "0")),
        password=os.environ.get("ROBOTHOR_REDIS_PASSWORD", ""),
    )

    ollama_cfg = OllamaConfig(
        host=os.environ.get("ROBOTHOR_OLLAMA_HOST", "127.0.0.1"),
        port=int(os.environ.get("ROBOTHOR_OLLAMA_PORT", "11434")),
        embedding_model=os.environ.get("ROBOTHOR_EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
        reranker_model=os.environ.get("ROBOTHOR_RERANKER_MODEL", "Qwen3-Reranker-0.6B:F16"),
        generation_model=os.environ.get("ROBOTHOR_GENERATION_MODEL", "nemotron-3-super"),
        vision_model=os.environ.get("ROBOTHOR_VISION_MODEL", "llama3.2-vision:11b"),
    )

    return Config(
        workspace=workspace,
        memory_dir=memory_dir,
        owner_name=os.environ.get("ROBOTHOR_OWNER_NAME", "there"),
        ai_name=os.environ.get("ROBOTHOR_AI_NAME", "Robothor"),
        db=db,
        redis=redis_cfg,
        ollama=ollama_cfg,
        bridge_port=int(os.environ.get("ROBOTHOR_BRIDGE_PORT", "9100")),
        orchestrator_port=int(os.environ.get("ROBOTHOR_ORCHESTRATOR_PORT", "9099")),
        vision_port=int(os.environ.get("ROBOTHOR_VISION_PORT", "8600")),
        helm_port=int(os.environ.get("ROBOTHOR_HELM_PORT", "3004")),
        engine_port=int(os.environ.get("ROBOTHOR_ENGINE_PORT", "18800")),
        tts_port=int(os.environ.get("ROBOTHOR_TTS_PORT", "8880")),
        voice_port=int(os.environ.get("ROBOTHOR_VOICE_PORT", "8765")),
        searxng_port=int(os.environ.get("ROBOTHOR_SEARXNG_PORT", "8888")),
        desktop_display=os.environ.get("ROBOTHOR_DESKTOP_DISPLAY", ":99"),
    )


def reset_config() -> None:
    """Reset the singleton config (for testing)."""
    global _config
    _config = None


def validate() -> list[tuple[str, bool, str]]:
    """Validate system configuration and connectivity.

    Returns list of (check_name, passed, detail) tuples.
    """
    import socket

    cfg = get_config()
    results: list[tuple[str, bool, str]] = []

    # 1. Required env vars
    required_env = {
        "OPENROUTER_API_KEY": "LLM API access",
        "ROBOTHOR_TELEGRAM_BOT_TOKEN": "Telegram bot",
        "ROBOTHOR_TELEGRAM_CHAT_ID": "Telegram delivery",
    }
    for var, purpose in required_env.items():
        val = os.environ.get(var, "")
        if val:
            results.append((f"env:{var}", True, purpose))
        else:
            results.append((f"env:{var}", False, f"{purpose} — not set"))

    # 2. Port checks — "in use" means service is running (good), "available" means not running (warning)
    for name, port in [
        ("bridge", cfg.bridge_port),
        ("orchestrator", cfg.orchestrator_port),
        ("engine", cfg.engine_port),
        ("vision", cfg.vision_port),
    ]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.bind(("127.0.0.1", port))
            s.close()
            results.append((f"port:{name}({port})", False, "not running (port available)"))
        except OSError:
            # Port in use — service is running
            results.append((f"port:{name}({port})", True, "running"))

    # 3. Database connectivity
    try:
        import psycopg2

        conn = psycopg2.connect(**cfg.db.dict, connect_timeout=3)
        conn.close()
        results.append(
            ("database", True, f"{cfg.db.name} on {cfg.db.host or 'socket'}:{cfg.db.port}")
        )
    except ImportError:
        results.append(("database", False, "psycopg2 not installed"))
    except Exception as e:
        results.append(("database", False, str(e)))

    # 4. Redis connectivity
    try:
        import redis as redis_lib

        r = redis_lib.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            db=cfg.redis.db,
            password=cfg.redis.password or None,
            socket_timeout=3,
        )
        r.ping()
        r.close()
        results.append(("redis", True, f"{cfg.redis.host}:{cfg.redis.port}"))
    except ImportError:
        results.append(("redis", False, "redis package not installed"))
    except Exception as e:
        results.append(("redis", False, str(e)))

    # 5. Ollama reachability
    try:
        import urllib.request

        req = urllib.request.Request(f"{cfg.ollama.base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            results.append(("ollama", True, cfg.ollama.base_url))
    except Exception as e:
        results.append(("ollama", False, str(e)))

    # 6. Service health
    import urllib.request

    for name, url in [
        ("bridge", cfg.bridge_url),
        ("orchestrator", cfg.orchestrator_url),
        ("vision", cfg.vision_url),
    ]:
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                results.append((f"service:{name}", True, f"{url}/health → {resp.status}"))
        except Exception as e:
            results.append((f"service:{name}", False, str(e)))

    # 7. File paths
    for label, path in [
        ("workspace", cfg.workspace),
        ("memory_dir", cfg.memory_dir),
    ]:
        p = Path(path)
        if p.exists():
            results.append((f"path:{label}", True, str(p)))
        else:
            results.append((f"path:{label}", False, f"{p} does not exist"))

    return results
