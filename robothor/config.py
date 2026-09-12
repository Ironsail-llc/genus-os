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


def _env_file_path() -> Path | None:
    """Resolve the workspace .env file path ``robothor init`` writes.

    Mirrors the workspace resolution ``_load_from_env`` uses below: the
    real ``ROBOTHOR_WORKSPACE`` env var if set, else ``~/robothor``.

    Never raises. ``Path.home()`` only runs when ``ROBOTHOR_WORKSPACE`` is
    unset (unlike a ``os.environ.get(..., Path.home() / ...)`` default,
    whose eager evaluation would call it every time). And even then, a
    ``RuntimeError`` from ``Path.home()`` — no ``HOME`` and no passwd entry
    for the running UID, a real container/k8s ``runAsUser`` case — is
    caught: there's no safe default workspace, so this returns ``None``
    and .env loading is skipped rather than crashing every importer of
    this module.
    """
    workspace_env = os.environ.get("ROBOTHOR_WORKSPACE")
    if workspace_env:
        return Path(workspace_env) / ".env"
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return home / "robothor" / ".env"


def _parse_env_file(text: str) -> dict[str, str]:
    """Minimal stdlib ``.env`` parser: ``KEY=VALUE`` lines only.

    Blank lines and ``#``-prefixed comments are ignored. Lines without an
    ``=`` or with an empty key are skipped. Surrounding whitespace is
    stripped from both key and value, and a value wrapped in a single
    matching pair of single or double quotes has them stripped. Never
    raises — arbitrary/malformed content just yields fewer entries.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _load_env_file(path: Path | None) -> None:
    """Load ``path`` into ``os.environ``, without ever overriding a value
    the real environment already set (e.g. systemd's ``EnvironmentFile``).

    ``path`` may be ``None`` (``_env_file_path()`` couldn't resolve a
    workspace) — nothing to load, which is silently fine, same as a
    missing or unreadable file. ``robothor init`` may never have run, or
    may have written to a different workspace. Safe to call more than
    once: already-set keys (including ones this function set on a prior
    call) are left alone.
    """
    if path is None:
        return
    try:
        text = path.read_text()
    except OSError:
        return
    for key, value in _parse_env_file(text).items():
        try:
            os.environ.setdefault(key, value)
        except ValueError:
            # e.g. an embedded NUL byte from truly malformed file content —
            # skip that one entry rather than let a bad line crash startup.
            continue


# Load the workspace .env file (if any) once at import time, before any
# config reader below consults os.environ. Real environment variables always
# win — this only fills in gaps, so it can never shadow systemd-injected
# production config.
_load_env_file(_env_file_path())


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL connection parameters."""

    host: str = ""  # empty = Unix socket (peer auth); set to 127.0.0.1 for TCP
    port: int = 5432
    name: str = "robothor_memory"
    user: str = "robothor"
    password: str = ""
    # Empty preserves libpq's platform default. Deployments should set this
    # explicitly; the production chart uses ``verify-full``.
    ssl_mode: str = ""

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
        if self.ssl_mode:
            parts.append(f"sslmode={self.ssl_mode}")
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
        if self.ssl_mode:
            d["sslmode"] = self.ssl_mode
        return d

    @property
    def url(self) -> str:
        """Return a libpq-compatible ``postgresql://`` URL.

        Tool subprocesses (e.g. ``psql $DATABASE_URL -c '...'`` inside
        ``experiment_measure`` metric commands) expect this format rather than
        the space-separated DSN.

        TCP connections use ``postgresql://user:pass@host:port/db``. Unix
        socket connections (``host=""`` or a socket directory path starting
        with ``/``) use the query-parameter form ``postgresql:///db?user=...``
        because the ``user@/db`` authority syntax is ambiguous in libpq — some
        versions parse the user as the dbname. Socket directories cannot sit
        in the URL authority at all; they must be a ``host=`` query param.
        """
        from urllib.parse import quote

        is_socket = not self.host or self.host.startswith("/")

        if not is_socket:
            userinfo = ""
            if self.user:
                userinfo = quote(self.user, safe="")
                if self.password:
                    userinfo = f"{userinfo}:{quote(self.password, safe='')}"
                userinfo = f"{userinfo}@"
            ssl_qs = f"?sslmode={quote(self.ssl_mode, safe='-')}" if self.ssl_mode else ""
            return f"postgresql://{userinfo}{self.host}:{self.port}/{self.name}{ssl_qs}"

        # Unix socket: put user, password, and socket dir (if set) in query.
        params: list[str] = []
        if self.user:
            params.append(f"user={quote(self.user, safe='')}")
        if self.password:
            params.append(f"password={quote(self.password, safe='')}")
        if self.host:
            params.append(f"host={quote(self.host, safe='/')}")
        if self.ssl_mode:
            params.append(f"sslmode={quote(self.ssl_mode, safe='-')}")
        qs = f"?{'&'.join(params)}" if params else ""
        return f"postgresql:///{self.name}{qs}"


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
    generation_model: str = "qwen3:8b"  # must agree with from_env below + env template + docs
    vision_model: str = "llama3.2-vision:11b"

    # Per-model-class keep_alive: how long models stay loaded after last use.
    # Embedding model is small (5.8GiB of 54GiB free) and on the hot path for
    # every memory write/read — never unload it (any negative duration pins it
    # forever), so a long 5xx/timeout storm can't starve it out mid-incident
    # (2026-08-18). NOTE: this is sent as a JSON *string*, and Ollama parses
    # string keep_alive with Go's time.ParseDuration — a bare "-1" is rejected
    # with HTTP 400 ("missing unit in duration"); it must carry a unit.
    # Reranker stays warm between 10-min cron cycles.
    # Large models (generation/vision) evict quickly to free memory.
    keep_alive_embedding: str = "-1m"
    keep_alive_reranker: str = "15m"
    keep_alive_generation: str = "5m"
    keep_alive_vision: str = "5m"
    # The AGENT FLEET's own residency, which is a different question from the
    # memory client's. The engine sent no keep_alive at all until 2026-08-27,
    # so the fleet's offline tier inherited the server's 10m default — shorter
    # than several agents' cron cadence, meaning every cron paid a ~34s cold
    # load and the model could be evicted between calls by memory traffic.
    keep_alive_engine: str = "30m"

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

    def _svc(self, port: int) -> str:
        return f"http://127.0.0.1:{port}"

    @property
    def engine_url(self) -> str:
        return self._svc(self.engine_port)

    @property
    def bridge_url(self) -> str:
        return self._svc(self.bridge_port)

    @property
    def orchestrator_url(self) -> str:
        return self._svc(self.orchestrator_port)

    @property
    def vision_url(self) -> str:
        return self._svc(self.vision_port)

    @property
    def voice_url(self) -> str:
        return self._svc(self.voice_port)

    @property
    def searxng_url(self) -> str:
        return self._svc(self.searxng_port)


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
        ssl_mode=os.environ.get("ROBOTHOR_DB_SSLMODE", ""),
    )
    # Export DATABASE_URL for subprocess tooling (e.g. experiment_measure
    # metric commands that shell out to psql). Respect an explicit override
    # so ops can point at a replica without editing code.
    if "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = db.url

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
        # qwen3:8b is THE default generation model, everywhere. This default
        # disagreed across four files (nemotron-3-super here, qwen3:32b in
        # llm/ollama.py, qwen3-next:latest in docs, qwen3:8b in the env
        # template), so what you got depended on which code path asked.
        generation_model=os.environ.get("ROBOTHOR_GENERATION_MODEL", "qwen3:8b"),
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


def probe_service(base_url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    """Ask a local service whether it is up, using an endpoint the CLI may reach.

    ``/ready`` is unauthenticated on every service that serves it, so it is
    tried first. ``/health`` is the fallback for services without ``/ready``;
    in production the bridge gates ``/health`` behind auth, so a 401/403 from
    it proves the service is up and enforcing auth, not that it is down.
    Returns ``(ok, detail)`` where *detail* names the URL and status seen.
    """
    import urllib.error
    import urllib.request

    last_detail = ""
    for path in ("/ready", "/health"):
        url = f"{base_url}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, f"{url} → {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return True, f"{url} → {e.code} (up, authenticated)"
            last_detail = f"{url} → {e.code}"
            if e.code == 404:
                continue
            return False, last_detail
        except Exception as e:  # URLError, timeout, connection refused
            return False, f"{url}: {e}"
    return False, last_detail or f"{base_url}: no /ready or /health"


def validate() -> list[tuple[str, bool, str]]:
    """Run the doctor and report it in this module's legacy tuple shape.

    A SHIM, and nothing else. This function used to be a second implementation
    of the same probes ``genus doctor`` now owns -- three environment
    variables, four ports, the database, redis, ollama, three service
    endpoints and two paths -- and once ``genus config validate`` became an
    alias for the doctor, nothing called it. A parallel implementation that
    nobody runs is the exact shape of every inert control this instance has
    shipped: it looks alive, it is tested, and it is not the thing that
    decides anything. So the body is gone and the name delegates.

    ``--offline``, for the same reason the alias does: this signature promises
    a cheap connectivity report, and a caller reaching for it must not start
    paying for completions.

    Returns ``(check id, ok, detail)``. ``ok`` is False only for a real
    failure; a SKIPPED check has not failed, and every caller of this shape
    reads False as broken -- so a skip comes back True with its status in the
    detail.
    """
    from robothor.doctor.context import DoctorContext
    from robothor.doctor.runner import run_sync

    report = run_sync(DoctorContext(offline=True))
    return [
        (
            row.id,
            row.status != "fail",
            row.detail if row.status != "skip" else f"skipped: {row.detail}",
        )
        for row in report.results
    ]
