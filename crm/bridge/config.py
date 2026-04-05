"""Bridge service configuration — reads from environment variables."""

import os
from pathlib import Path

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

# Database (used by crm_dal.py for backward compat — new code uses robothor.db.connection)
PG_DSN = os.getenv(
    "PG_DSN",
    f"dbname={os.getenv('ROBOTHOR_DB_NAME', 'robothor_memory')} "
    f"user={os.getenv('ROBOTHOR_DB_USER', 'robothor')} "
    f"host={os.getenv('ROBOTHOR_DB_HOST', '/var/run/postgresql')}",
)
