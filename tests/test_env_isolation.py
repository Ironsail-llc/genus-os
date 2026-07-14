"""The suite must not read — or keep — the host's live configuration.

`robothor.cli.main()` calls `load_instance_env()`, which adopts the instance's
systemd drop-in environment. That is correct and deliberate for a real CLI run:
without it, a shell (or an agent shelling out) reads every rollout-gated
guardrail back as off/observe while the daemon enforces it.

But it mutates the **process-global** `os.environ`. So any test that invokes the
CLI silently imports whatever the developer's machine happens to have configured
— and leaves it there for every test that follows.

That is exactly how it bit: adding `ROBOTHOR_SANDBOX_BINARY=podman` to this box's
engine drop-in made `tests/test_setup.py::TestCliInit::test_init_help` poison
`robothor/engine/tests/test_sandbox.py::test_docker_exec`, which then asserted
`'podman' == 'docker'`. Nothing about the test changed. The *host* changed.

CI never saw it, because CI has no drop-in. A suite whose result depends on the
machine it runs on is not a suite — it is a coincidence.
"""

from __future__ import annotations

import os

import pytest

_SENTINEL = "ROBOTHOR_HERMETIC_SENTINEL"


class TestTheSuiteIsHermetic:
    """These two run in order. The first dirties os.environ; the second must not
    see it. That is the whole contract — one test cannot reconfigure the next."""

    def test_a_dirties_the_environment(self) -> None:
        os.environ[_SENTINEL] = "leaked"
        # Also do the real thing: invoke the CLI, which adopts the host's systemd
        # drop-in env via load_instance_env().
        from robothor.cli import main

        with pytest.raises(SystemExit):
            main(["init", "--help"])

    def test_b_does_not_see_it(self) -> None:
        assert _SENTINEL not in os.environ, (
            "os.environ leaked between tests. The autouse _hermetic_env fixture in "
            "conftest.py must snapshot and restore it — otherwise robothor.cli.main(), "
            "which adopts the instance's systemd drop-in, silently reconfigures every "
            "test that follows with whatever THIS machine happens to have set. That is "
            "how test_init_help poisoned test_docker_exec with a live "
            "ROBOTHOR_SANDBOX_BINARY=podman."
        )
