"""A deterministic stand-in for every model endpoint a fresh install touches."""

from tests.acceptance.mock_llm.app import EMBEDDING_DIMENSIONS, app, embedding_for

__all__ = ["EMBEDDING_DIMENSIONS", "app", "embedding_for"]
