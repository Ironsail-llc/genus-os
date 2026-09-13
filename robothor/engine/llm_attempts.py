"""What one LLM attempt did, and where that evidence is kept.

Two things the retry loop used to throw away (DIAG 2026-09-13):

**§4.4 — the evidence.** ``llm_client`` raised ``EmptyCompletionError`` and
discarded the response without reading ``usage`` or ``finish_reason``, and the
runner recorded ONE ``agent_run_steps`` row per ``_do_llm_call`` built from the
FINAL successful response — so ``duration_ms`` silently covered every failed
attempt and every backoff, and a failed attempt had no row at all. Measured
consequence: ``zero_out = 0`` on every day of a week in which the engine
re-rolled ~50 empty completions a day. The failure the fleet hit 50x/day was
not describable from the database.

**§4.1 — the distinction.** A response with blank ``content`` but reasoning on
it is a thinking model that spent its budget before the answer started. It is
not a provider empty, and an identical re-roll truncates identically. The
fields are the ones ``reasoning_replay.REASONING_FIELDS`` already defines.

The sink is a ContextVar rather than a parameter threaded through the call
chain: ``_call_llm`` and ``_call_llm_streaming`` are both awaited inside the
runner's own task, and an attempt record is diagnostics — it must never change
a signature that a provider failure has to survive.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from robothor.engine.reasoning_replay import REASONING_FIELDS

if TYPE_CHECKING:
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)

#: Outcome names. They are written verbatim into ``agent_run_steps`` on a
#: failed attempt, so they are greppable and must not be reworded casually.
OUTCOME_SUCCESS: Final = "success"
OUTCOME_EMPTY: Final = "empty"
OUTCOME_REASONING_ONLY: Final = "reasoning_only"
#: A reasoning-only reply the SAME model was immediately re-asked about, and
#: which therefore cost latency and nothing else. Named apart from
#: ``reasoning_only`` (which advanced the chain) because every consumer that
#: asks "did this run go wrong?" must be able to tell a self-heal from a
#: failure without parsing prose.
OUTCOME_REASONING_ONLY_RETRY: Final = "reasoning_only_retry"
OUTCOME_TIMEOUT: Final = "timeout"
OUTCOME_ERROR: Final = "error"

#: Reasoning effort for the same re-ask on paths that speak OpenAI's knob
#: (compaction calls litellm directly and builds no thinking block). The agent
#: loop reduces its own thinking budget instead — see
#: ``llm_client._thinking_kwargs``.
REASONING_ONLY_RETRY_EFFORT: Final = "low"

REASONING_ONLY_NUDGE: Final = (
    "Your previous reply contained reasoning but no answer and no tool call. "
    "Think briefly, then reply with the answer itself (or the tool call) this time."
)

#: A run that recorded an unbounded number of attempts would be a second
#: outage. One chain, its retries and its key rotations cannot reach this.
_MAX_RECORDED_ATTEMPTS: Final = 32


@dataclass(frozen=True)
class CompletionShape:
    """What a provider actually returned, in the fields the loop must judge.

    ``parsed`` is False for any response shape we do not recognise. Every
    verdict below is gated on it, so an unfamiliar shape can never drive a
    retry loop — the same rule ``_is_empty_completion`` has held since
    2026-08-22.
    """

    parsed: bool = False
    finish_reason: str | None = None
    content_present: bool = False
    tool_calls_present: bool = False
    reasoning_present: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def no_answer(self) -> bool:
        """Neither text nor a tool call: this turn produced nothing usable."""
        return self.parsed and not self.content_present and not self.tool_calls_present

    @property
    def reasoning_only(self) -> bool:
        """No answer, but the model did reason — §4.1's population."""
        return self.no_answer and self.reasoning_present

    @property
    def empty(self) -> bool:
        """No answer and no reasoning: a real provider empty."""
        return self.no_answer and not self.reasoning_present

    @property
    def outcome(self) -> str:
        if not self.no_answer:
            return OUTCOME_SUCCESS
        return OUTCOME_REASONING_ONLY if self.reasoning_present else OUTCOME_EMPTY

    def describe(self) -> str:
        """The string an operator greps at 02:00."""
        return (
            f"finish_reason={self.finish_reason}, "
            f"output_tokens={self.output_tokens}, "
            f"reasoning_tokens={self.reasoning_tokens}, "
            f"reasoning_present={self.reasoning_present}"
        )


def _reasoning_present(message: Any) -> bool:
    return any(getattr(message, field, None) for field in REASONING_FIELDS)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def describe_completion(result: Any) -> CompletionShape:
    """Read the fields the response carries. Never raises."""
    try:
        choice = result.choices[0]
        message = choice.message
    except (AttributeError, IndexError, TypeError):
        return CompletionShape()

    content = getattr(message, "content", None)
    content_present = bool(content.strip()) if isinstance(content, str) else content is not None
    usage = getattr(result, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return CompletionShape(
        parsed=True,
        finish_reason=getattr(choice, "finish_reason", None),
        content_present=content_present,
        tool_calls_present=bool(getattr(message, "tool_calls", None)),
        reasoning_present=_reasoning_present(message),
        input_tokens=_int_or_none(getattr(usage, "prompt_tokens", None)),
        output_tokens=_int_or_none(getattr(usage, "completion_tokens", None)),
        reasoning_tokens=_int_or_none(getattr(details, "reasoning_tokens", None)),
    )


@dataclass(frozen=True)
class LLMAttempt:
    """One provider call: what it cost and how it ended."""

    model: str
    outcome: str
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.outcome != OUTCOME_SUCCESS

    def error_message(self) -> str | None:
        """The ``agent_run_steps.error_message`` text for a failed attempt."""
        if not self.failed:
            return None
        return f"{self.outcome}: {self.detail}" if self.detail else self.outcome


_attempts: ContextVar[list[LLMAttempt] | None] = ContextVar("llm_attempts", default=None)


def begin_attempts() -> None:
    """Arm the per-call sink. Any previous, unclaimed sink is dropped."""
    _attempts.set([])


def take_attempts() -> list[LLMAttempt]:
    """Claim the recorded attempts and disarm the sink."""
    recorded = _attempts.get() or []
    _attempts.set(None)
    return list(recorded)


def record_attempt(attempt: LLMAttempt) -> None:
    """Record one attempt, if a caller armed the sink."""
    sink = _attempts.get()
    if sink is None or len(sink) >= _MAX_RECORDED_ATTEMPTS:
        return
    sink.append(attempt)


def note_outcome(
    model: str,
    started: float,
    *,
    shape: CompletionShape | None = None,
    error: BaseException | None = None,
    self_healed: bool = False,
) -> None:
    """Record the attempt that just finished. Diagnostics only — never raises.

    ``self_healed`` marks a reasoning-only reply the caller is about to re-ask
    on the same model, so the row says ``reasoning_only_retry`` rather than
    ``reasoning_only``.
    """
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if error is not None:
        record_attempt(
            LLMAttempt(
                model=model,
                outcome=classify_error(error),
                duration_ms=duration_ms,
                detail=f"{type(error).__name__}: {error}"[:300],
            )
        )
        return
    shape = shape if shape is not None else CompletionShape()
    outcome = OUTCOME_REASONING_ONLY_RETRY if self_healed else shape.outcome
    record_attempt(
        LLMAttempt(
            model=model,
            outcome=outcome,
            duration_ms=duration_ms,
            input_tokens=shape.input_tokens or 0,
            output_tokens=shape.output_tokens or 0,
            finish_reason=shape.finish_reason,
            detail=None if outcome == OUTCOME_SUCCESS else shape.describe(),
        )
    )


def classify_error(error: BaseException) -> str:
    """The failure class an operator sorts by."""
    if isinstance(error, TimeoutError):
        return OUTCOME_TIMEOUT
    status = getattr(error, "status_code", None)
    return f"{OUTCOME_ERROR}_{status}" if status else OUTCOME_ERROR


def is_attempt_step(step: Any) -> bool:
    """True for a failed-attempt row, in either a RunStep or a database row.

    The invariant every consumer rests on: ``AgentSession.record_llm_call``
    never sets ``error_message``, so an ``llm_call`` step that carries one was
    written by ``record_llm_attempt`` and is telemetry about a retry — not a
    step of the agent's work, not a turn, and NOT a run error. Counting these
    as errors would hand the verifier and the goal judge a failure on the very
    path this module exists to make visible (hostile review of #531, C1/I1).
    """
    if isinstance(step, dict):
        step_type: Any = step.get("step_type")
        error_message = step.get("error_message")
    else:
        step_type = getattr(step, "step_type", None)
        error_message = getattr(step, "error_message", None)
    return str(getattr(step_type, "value", step_type)) == "llm_call" and bool(error_message)


def record_attempt_steps(session: AgentSession, attempts: list[LLMAttempt]) -> int:
    """Write one ``agent_run_steps`` row per FAILED attempt.

    The successful attempt keeps the existing row the caller writes with the
    assistant turn on it; only the failures were missing. Returns the duration
    of the successful attempt, or 0 when there was none — the caller uses it so
    the success row's ``duration_ms`` stops covering the retries (DIAG §2.2).
    """
    success_ms = 0
    for attempt in attempts:
        if not attempt.failed:
            success_ms = attempt.duration_ms
            continue
        try:
            session.record_llm_attempt(
                model=attempt.model,
                duration_ms=attempt.duration_ms,
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                error_message=attempt.error_message(),
            )
        except Exception as e:  # noqa: BLE001 - telemetry must not fail a run
            logger.debug("could not record a failed LLM attempt: %s", e)
    return success_ms
