# Contributing to Genus OS

Thank you for your interest in contributing to Genus OS! This document covers the development setup, coding standards, and contribution process.

## Development Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 16+ with pgvector extension
- Redis 7+
- Ollama (for LLM features)

### Quick Start

```bash
# Clone and install
git clone https://github.com/genusos/genusos.git
cd genus-os
pip install -e ".[dev]"

# Set up pre-commit hooks
pip install pre-commit
pre-commit install

# Run tests
pytest

# Lint and type check
ruff check .
mypy robothor/
```

> **Pre-commit hooks** run gitleaks (secret scanning), ruff (lint + format), and agent manifest validation automatically on every commit. If a hook fails, fix the issue and re-commit.

### Full Stack (Docker)

```bash
docker-compose up -d  # PostgreSQL, Redis, Ollama
robothor migrate      # Run database migrations
pytest -m integration # Run integration tests
```

## Code Style

We use **ruff** with default settings. No manual formatting needed.

```bash
ruff check .       # Lint
ruff format .      # Format
```

Key conventions:
- **Line length:** 100 characters
- **Imports:** sorted by `isort` (via ruff)
- **Type hints:** encouraged but not mandatory for private functions
- **Docstrings:** required for public functions and classes

## The Extract-Before-Import Rule

Every new line of code imports from `robothor.*`, not raw files with `sys.path.insert`. If a module hasn't been extracted to the package yet, extract it first.

```python
# Good
from robothor.memory.facts import search_facts
from robothor.config import get_config

# Bad
import sys
sys.path.insert(0, "/path/to/memory_system")
from fact_extraction import search_facts
```

## Testing

### Markers

| Marker | Meaning | Speed |
|--------|---------|-------|
| *(none)* | Unit test - mocked deps, no I/O | <1s |
| `@pytest.mark.integration` | Real DB/Redis | <10s |
| `@pytest.mark.llm` | Needs Ollama running | varies |
| `@pytest.mark.slow` | >10s wall time | >10s |
| `@pytest.mark.e2e` | Full system end-to-end | >30s |
| `@pytest.mark.smoke` | Health check only | <3s |

### Test Guidelines

- **Test AI by properties, not values.** Validate structure (fields present, types correct, values in valid ranges), not exact LLM output.
- **Tests live alongside code.** `robothor/<module>/tests/test_<feature>.py`
- **Use `test_prefix` fixture** for database isolation in integration tests.
- **Mock LLMs in unit tests.** Only use real LLM calls in `@pytest.mark.llm` tests.

### Running Tests

```bash
# Fast (pre-commit)
pytest -m "not slow and not llm and not e2e"

# Full suite
pytest

# Single module
pytest robothor/memory/tests/ -v
```

## Pull Request Process

1. **Fork** the repo and create your branch from `main`
2. **Write tests** for any new functionality
3. **Run the test suite** and ensure it passes
4. **Update documentation** if you changed APIs or behavior
5. **Open a PR** — the [PR template](.github/PULL_REQUEST_TEMPLATE.md) will guide you through the checklist

All PRs are **squash-merged** into `main` to keep history clean. Write a clear PR title — it becomes the commit message.

### AI-Generated PRs

AI-generated contributions are welcome. We just ask that you check the "AI-assisted" box in the PR template and note which tool you used. This is informational, not a gate.

### PR Title Format

Use conventional commit prefixes:
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation only
- `test:` Adding or updating tests
- `refactor:` Code change that neither fixes a bug nor adds a feature
- `chore:` Build process or auxiliary tool changes

### What We Look For

- Tests pass on all supported Python versions (3.11, 3.12, 3.13)
- No ruff warnings
- Clear commit messages
- Documentation updated for user-facing changes
- No secrets or personal data in commits

## Architecture Overview

Genus OS is a three-layer system in a single repo:

1. **Intelligence Layer** (`robothor/`) — Python package: memory, RAG, knowledge graph, CRM, vision, events, federation
2. **Agent Engine** (`robothor/engine/`) — Python agent execution with 17 autonomous agents defined as YAML manifests
3. **Control Plane** (`app/`) — The Helm, a Next.js 16 live dashboard with chat, task board, and event streams
4. **Federation** (`robothor/federation/`) — Peer-to-peer instance networking over NATS JetStream

Supporting services: Bridge (`crm/bridge/`) for CRM API, PostgreSQL+pgvector, Redis, Ollama, NATS, 20 systemd units.

## Getting Help

- Open an issue for bugs or feature requests
- Start a discussion for questions or ideas
- Check existing issues before creating new ones
