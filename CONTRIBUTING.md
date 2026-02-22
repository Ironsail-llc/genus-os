# Contributing to Robothor

Thank you for your interest in contributing to Robothor! This document covers the development setup, coding standards, and contribution process.

## Development Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 16+ with pgvector extension
- Redis 7+
- Ollama (for LLM features)

### Quick Start

```bash
# Clone and install
git clone https://github.com/robothor-ai/robothor.git
cd robothor
pip install -e ".[dev]"

# Run tests
pytest

# Lint and type check
ruff check .
mypy robothor/
```

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
5. **Open a PR** with a clear description of changes

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

Robothor is a three-layer system:

1. **Intelligence Layer** (`robothor/`) - Python package: memory, RAG, knowledge graph, CRM, vision
2. **Agent Orchestration** - OpenClaw or any agent framework (consumes `robothor` as a library)
3. **Infrastructure** - PostgreSQL+pgvector, Redis, Ollama, systemd

The `robothor` package is the intelligence layer. It provides the brain. Agent frameworks provide the body.

## Getting Help

- Open an issue for bugs or feature requests
- Start a discussion for questions or ideas
- Check existing issues before creating new ones
