"""A sandbox that cannot start must not silently run on the host.

`ROBOTHOR_SANDBOX_DEFAULT_MODE=enforce` promises that exec-holding agents run
inside a Docker sandbox. Today, if the sandbox fails to start — no image, no
socket permission, daemon down — the runner logs an error, sets `sandbox = None`
and **continues on the host**. The control degrades open: the operator believes
exec is contained when it is not.

That is not theoretical on this instance: the engine runs as a user who is not
in the `docker` group, so `Sandbox.start()` cannot succeed at all. Flipping the
flag to enforce today would produce error noise and zero containment.

Under `enforce`, a sandbox that cannot be created must fail the run.
Under `observe`, degrading to the host is correct — that is what observe means.
"""

from __future__ import annotations

import re
from pathlib import Path

import robothor.engine.runner as runner_mod


def _sandbox_block() -> str:
    src = Path(runner_mod.__file__).read_text()
    start = src.index('if _sb_decision == "docker":')
    end = src.index("# Watchdog already started", start)
    return src[start:end]


def test_enforce_does_not_degrade_to_host_on_sandbox_failure():
    block = _sandbox_block()

    # the except path must distinguish enforce from observe
    assert re.search(
        r'sandbox_default_mode\(\)\s*==\s*"enforce"|_sb_mode\s*==\s*"enforce"', block
    ), (
        "the sandbox-start failure path does not check the enforcement mode — "
        "it sets sandbox = None and continues on the host in every mode, so "
        "'enforce' silently runs exec agents unsandboxed when Docker is "
        "unavailable (which is the case on this box: the engine user is not in "
        "the docker group)"
    )


def test_failure_under_enforce_records_a_guardrail_event():
    block = _sandbox_block()
    assert "log_guardrail_event" in block or 'guardrail_name="sandbox_default"' in block, (
        "a sandbox that could not be created under enforce must leave an audit "
        "trail, not just a log line"
    )
