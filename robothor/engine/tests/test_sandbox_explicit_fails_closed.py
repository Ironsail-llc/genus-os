"""An agent that explicitly asks for the container must never run on the host.

`_resolve_sandbox_decision` treats a manifest `sandbox: docker` as an absolute:
"explicit manifest ``sandbox: docker`` always sandboxes". But the failure path
only fails closed when the *global* flag is `enforce`:

    if sandbox_default_mode() == "enforce":
        ... block the run ...
    # otherwise: sandbox = None, and the run continues ON THE HOST

So with the global flag at `off`/`observe` — which is where it sits — an agent
whose manifest explicitly says `sandbox: docker` silently degrades to the host
the moment the container fails to start. Observed on the live box: auto-agent,
manifest `sandbox: docker`, container start failed, run continued on the host.
Nothing surfaced.

The global mode is a *default* for agents that never asked. It has no business
overriding an agent that did. Two different questions:

  * "should agents that never asked be contained?"  -> the global flag
  * "this agent asked to be contained; it isn't"    -> always a failure

Only the second is what this pins.
"""

from __future__ import annotations

import inspect
import re

from robothor.engine import runner


def _sandbox_failure_block() -> str:
    """The `except` arm of the sandbox-start try in the run loop."""
    src = inspect.getsource(runner)
    start = src.index("Sandbox start failed for")
    return src[start : start + 2000]


def test_explicit_sandbox_docker_fails_closed_regardless_of_global_mode() -> None:
    block = _sandbox_failure_block()

    # The guard must consider the agent's own config, not only the global flag.
    considers_agent_config = re.search(
        r"agent_config\.sandbox\s*==\s*[\"']docker[\"']|_sb_explicit|explicit",
        block,
    )
    assert considers_agent_config, (
        "the sandbox-start failure path branches only on sandbox_default_mode() == "
        "'enforce'. An agent whose manifest explicitly says `sandbox: docker` will "
        "silently run on the HOST whenever the global flag is off/observe — which is "
        "where it sits. Explicit opt-in must fail closed on its own."
    )


def test_the_failure_is_still_recorded() -> None:
    """Failing closed silently is only half of it — the block must leave evidence."""
    block = _sandbox_failure_block()
    assert "log_guardrail_event" in block, (
        "a run blocked for lack of a sandbox must write a guardrail event, or the "
        "operator sees a failed run with no reason"
    )
