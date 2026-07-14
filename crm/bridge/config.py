"""Bridge service configuration — reads from environment variables."""

import os
from pathlib import Path

from robothor.config import get_config

# Try to load .env if dotenv is available (dev convenience)
try:
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# Service URLs
MEMORY_URL = os.getenv("MEMORY_URL", "http://localhost:9099")

# Impetus One
IMPETUS_ONE_URL = os.getenv("IMPETUS_ONE_BASE_URL", "http://localhost:8000")
IMPETUS_ONE_TOKEN = os.getenv("IMPETUS_ONE_API_TOKEN", "")

# Database (used by crm_dal.py for backward compat — new code uses
# robothor.db.connection). Reuse the canonical builder so port, password, and
# TLS policy are not silently dropped in container deployments.
_DEFAULT_PG_DSN = get_config().db.dsn
PG_DSN = os.getenv(
    "PG_DSN",
    _DEFAULT_PG_DSN,
)
