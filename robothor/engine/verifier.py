"""
Self-Validation — optional post-execution verification step.

After the main execution loop, evaluates whether the output meets success
criteria. Uses a separate LLM call with JSON mode. If verification fails,
the agent gets one retry with feedback injected.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import litellm

logger = logging.getLogger(__name__)

DEFAULT_CRITERIA = "Task completed successfully without errors."

VERIFICATION_PROMPT = """Evaluate whether this agent's execution met the success criteria.

Success criteria: {criteria}

Agent's final output:
{output}

Errors during execution: {error_count}

Respond with ONLY valid JSON:
{{
  "passed": true or false,
  "confidence": 0.0 to 1.0,
  "issues": ["list of issues found"],
  "suggestions": ["list of improvements"]
}}"""


@dataclass
class VerificationResult:
    """Result of the verification step."""

    passed: bool = True
    confidence: float = 1.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    error: str | None = None


async def verify_output(
    output_text: str,
    criteria: str,
    error_count: int,
    model: str,
    fallback_models: list[str] | None = None,
) -> VerificationResult:
    """Run verification on agent output. Non-fatal — returns pass on failure."""
    if not output_text:
        return VerificationResult(
            passed=False,
            confidence=0.0,
            issues=["No output produced"],
        )

    criteria = criteria or DEFAULT_CRITERIA
    prompt = VERIFICATION_PROMPT.format(
        criteria=criteria,
        output=output_text[:3000],
        error_count=error_count,
    )

    models = [model] + (fallback_models or [])
    models = [m for m in models if m]

    for m in models:
        try:
            response = await litellm.acompletion(
                model=m,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                continue

            data = json.loads(content)
            return VerificationResult(
                passed=bool(data.get("passed", True)),
                confidence=float(data.get("confidence", 0.5)),
                issues=data.get("issues", []),
                suggestions=data.get("suggestions", []),
            )
        except Exception as e:
            logger.debug("Verification failed with model %s: %s", m, e)
            continue

    # If all models fail, pass by default (non-fatal)
    return VerificationResult(error="All verification models failed")


def format_verification_feedback(result: VerificationResult) -> str:
    """Format verification failure as feedback for retry."""
    lines = ["[VERIFICATION FAILED]"]
    if result.issues:
        lines.append("Issues found:")
        for issue in result.issues:
            lines.append(f"  - {issue}")
    if result.suggestions:
        lines.append("Suggestions:")
        for s in result.suggestions:
            lines.append(f"  - {s}")
    lines.append("Please address these issues and try again.")
    return "\n".join(lines)
