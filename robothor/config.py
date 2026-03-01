"""
Centralized configuration for Robothor.

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
    generation_model: str = "qwen3-next:latest"
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
    """Top-level Robothor configuration."""

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

    @property
    def bridge_url(self) -> str:
        return f"http://127.0.0.1:{self.bridge_port}"

    @property
    def orchestrator_url(self) -> str:
        return f"http://127.0.0.1:{self.orchestrator_port}"

    @property
    def vision_url(self) -> str:
        return f"http://127.0.0.1:{self.vision_port}"


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
        generation_model=os.environ.get("ROBOTHOR_GENERATION_MODEL", "qwen3-next:latest"),
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
    )


def reset_config() -> None:
    """Reset the singleton config (for testing)."""
    global _config
    _config = None
