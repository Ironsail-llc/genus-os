"""How long one LLM call may take, and how many times the chain walk retries it.

A leaf. It imports nothing from the engine and nothing from litellm, and that
is the whole point: these numbers are the input to two very different things.

* ``llm_client`` spends them — they become the ``timeout`` kwarg on every
  provider call and the attempt counts of the chain walk. It re-exports every
  name here, so ``from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT``
  keeps working for the couple of dozen callers that already do it.
* ``workflow_budget`` *predicts* them, to answer "can one step of this workflow
  outspend the whole workflow?" before anything runs.

They lived only in ``llm_client`` until 2026-09-13, which had two costs. The
prediction side re-derived the local/cloud split from the model prefix and
immediately drifted from the runtime (it matched a bare ``ollama/``, the
runtime did not). And the validator that wants to answer the question in CI
could not, because reaching a 300 and a 600 meant importing litellm into a job
whose entire dependency list is ``pyyaml``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _timeout_from_env(name: str, default: int) -> int:
    """Read a positive-integer timeout from the environment, or the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using default %ds", name, raw, default)
        return default
    return value if value > 0 else default


#: Per-call allowance for an interactive trigger. Wraps each provider call so
#: the runner cancels and falls through if the provider hangs: the ``timeout``
#: kwarg litellm is given is best-effort and was observed silently ignored,
#: causing 1800s stalls against codex/gpt-5.5 in the 2026-05-28 incident.
LLM_REQUEST_TIMEOUT = _timeout_from_env("ROBOTHOR_LLM_TIMEOUT", 120)

#: Non-interactive (cron/workflow) runs get more headroom: nobody is waiting
#: on the reply, and a large classify context on a reasoning model can
#: legitimately take >120s of wall-clock generation (2026-08-20: all three
#: chain models were cancelled at exactly 120s while small concurrent calls
#: succeeded — the chain exhausted on slowness, not provider death).
LLM_REQUEST_TIMEOUT_BATCH = _timeout_from_env("ROBOTHOR_LLM_TIMEOUT_BATCH", 300)

#: The on-device tier's own allowance — cold-start model loads are slow.
LLM_REQUEST_TIMEOUT_OLLAMA = 600

#: Trigger types whose runs are batch-shaped (no human waiting on the reply)
#: and therefore get LLM_REQUEST_TIMEOUT_BATCH per model.
BATCH_TRIGGER_TYPES = frozenset({"cron", "workflow"})

#: One in-place retry per model for transient failures (timeout / 5xx) before
#: advancing the fallback chain. A transient 502 used to burn the model's only
#: attempt and exhaust the whole chain within minutes.
TRANSIENT_RETRIES_PER_MODEL = 1

#: Extra in-place retries for the on-device tier when it answers "busy".
#: Every agent's chain ends in one local model served with a small parallel
#: slot count, so during a cloud outage the whole fleet arrives at once and
#: the queue fills. That is backpressure, not death: it drains in seconds,
#: and one retry throws away the only tier still answering.
LOCAL_CAPACITY_RETRIES = 4


def is_local_model(model: str) -> bool:
    """Is this served on-device, with no credential and no provider account?"""
    return model.startswith(("ollama_chat/", "ollama/"))


def uses_ollama_timeout(model: str) -> bool:
    """Does this model get ``LLM_REQUEST_TIMEOUT_OLLAMA`` for a single call?

    Deliberately NARROWER than :func:`is_local_model`, which also matches a
    bare ``ollama/``. That difference is long-standing runtime behaviour in
    ``llm_client._per_call_timeout``; this function records it rather than
    silently changing it, and gives the prediction side something to agree
    with instead of a second copy of the prefix test to drift from.
    """
    return model.startswith("ollama_chat/")


def model_call_allowance(model: str, *, batch: bool = True) -> int:
    """Seconds ONE call to ``model`` may take, as the chain walk will allow it.

    ``batch`` reflects the trigger: workflow and cron runs are batch-shaped
    (see :data:`BATCH_TRIGGER_TYPES`) and get the higher non-interactive value.
    """
    if uses_ollama_timeout(model):
        return LLM_REQUEST_TIMEOUT_OLLAMA
    return LLM_REQUEST_TIMEOUT_BATCH if batch else LLM_REQUEST_TIMEOUT


def in_place_retries(model: str) -> int:
    """In-place retries the chain walk grants ``model`` before advancing.

    Two different constants, chosen by tier — the same branch
    ``llm_client._call_llm`` takes when it computes ``attempts``.
    """
    return LOCAL_CAPACITY_RETRIES if is_local_model(model) else TRANSIENT_RETRIES_PER_MODEL
