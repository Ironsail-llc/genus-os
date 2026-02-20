"""Bridge service configuration."""
import os
from dotenv import load_dotenv

load_dotenv("/home/philip/robothor/crm/.env")

# Service URLs
MEMORY_URL = "http://localhost:9099"

# Impetus One
IMPETUS_ONE_URL = os.getenv("IMPETUS_ONE_BASE_URL", "http://localhost:8000")
IMPETUS_ONE_TOKEN = os.getenv("IMPETUS_ONE_API_TOKEN", "")

# Database
PG_DSN = "dbname=robothor_memory user=philip host=/var/run/postgresql"
