"""Tests for the self-validation verifier."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.verifier import (
    VerificationResult,
    format_verification_feedback,
    verify_output,
)


@pytest.mark.asyncio
async def test_verify_output_passes():
    """Verification passes when LLM says it passed."""
    verification_data = {
        "passed": True,
        "confidence": 0.95,
        "issues": [],
        "suggestions": [],
    }
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(verification_data)

    with patch("litellm.acompletion", return_value=response):
        result = await verify_output("Task done", "Task completed", 0, "test-model")

    assert result.passed is True
    assert result.confidence == 0.95


@pytest.mark.asyncio
async def test_verify_output_fails():
    """Verification fails when LLM reports issues."""
    verification_data = {
        "passed": False,
        "confidence": 0.3,
        "issues": ["Missing output file"],
        "suggestions": ["Write the output file"],
    }
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(verification_data)

    with patch("litellm.acompletion", return_value=response):
        result = await verify_output("Partial", "Complete task", 2, "test-model")

    assert result.passed is False
    assert "Missing output file" in result.issues


@pytest.mark.asyncio
async def test_verify_output_empty_output():
    """Empty output always fails verification."""
    result = await verify_output("", "Task done", 0, "test-model")
    assert result.passed is False
    assert "No output" in result.issues[0]


@pytest.mark.asyncio
async def test_verify_output_all_models_fail():
    """Returns pass-by-default when all models fail."""
    with patch("litellm.acompletion", side_effect=Exception("API error")):
        result = await verify_output("Output", "Criteria", 0, "bad-model")

    assert result.passed is True  # pass by default
    assert result.error is not None


def test_format_verification_feedback():
    """Formats verification failure as readable feedback."""
    result = VerificationResult(
        passed=False,
        issues=["Missing file", "Wrong format"],
        suggestions=["Create the file"],
    )
    text = format_verification_feedback(result)
    assert "[VERIFICATION FAILED]" in text
    assert "Missing file" in text
    assert "Create the file" in text
