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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig
    from robothor.engine.session import AgentSession

# Dial through the credential pool. A direct litellm call lets the SDK
# resolve the provider key from the environment, which on 2026-08-27 meant
# this path kept hammering a credential the pool had already retired and
# could not rotate to a spare. No-op for unpooled providers.
from robothor.engine.pooled_completion import acompletion as pooled_acompletion
from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)

DEFAULT_CRITERIA = (
    "The task was carried out and the final output is the deliverable the "
    "task calls for. Open items, pending decisions, blockers or problems the "
    "agent reports to the operator are content of that deliverable, not "
    "failures; judge only whether the deliverable itself is present and "
    "coherent."
)

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
            response = await pooled_acompletion(
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
            logger.debug(
                "Verification failed with model %s: %s",
                sanitize_log(m),
                sanitize_log(str(e)),
            )
            continue

    # If all models fail, pass by default (non-fatal)
    return VerificationResult(error="All verification models failed")


def format_verification_feedback(result: VerificationResult) -> str:
    """Format verification failure as feedback for retry."""
    lines = ["[VERIFICATION FAILED]"]
    if result.issues:
        lines.append("Issues found:")
        lines.extend(f"  - {issue}" for issue in result.issues)
    if result.suggestions:
        lines.append("Suggestions:")
        lines.extend(f"  - {s}" for s in result.suggestions)
    lines.append(
        "Your previous reply was judged against the success criteria above. "
        "Respond now with the complete deliverable itself — the full report or "
        "answer the task requires — revised to address the issues. Do not reply "
        "to this feedback, do not discuss the verification, and do not summarize "
        "or refer to what you already sent: the next message is what the "
        "recipient receives."
    )
    return "\n".join(lines)


def should_verify(
    agent_config: AgentConfig, route: Any, session: AgentSession | None = None
) -> bool:
    """Determine if verification step should run."""
    from robothor.engine.models import TriggerType
    from robothor.goals.runtime import pursuit_yielded

    if session and pursuit_yielded(session.run):
        return False
    if agent_config.verification_enabled:
        return True
    # Skip verification for interactive sessions (adds latency, Qwen JSON unreliable)
    if (
        session
        and session.run
        and session.run.trigger_type
        in (
            TriggerType.TELEGRAM,
            TriggerType.WEBCHAT,
        )
    ):
        return False
    # Skip verification for heartbeat (scout) runs — the scout is a
    # deterministic scan-and-file beat with a rigid 5-section digest
    # format. Verification second-guesses the digest, provokes the model
    # into writing a meta-defense, and that defense overwrites the real
    # output_text — operator ends up seeing garbage instead of the digest.
    if (
        session
        and session.run
        and session.run.trigger_detail
        and session.run.trigger_detail.startswith("heartbeat:")
    ):
        return False
    return bool(route and route.verification is True)
