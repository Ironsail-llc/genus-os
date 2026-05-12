"""Top-level import canary.

One job: fail loudly if `robothor.engine.daemon` or `robothor.engine.runner`
have broken top-level imports. Added 2026-04-24 after an agent's write_file
rewrote runner.py with hallucinated ``robothor.agent.*`` imports and the
engine crash-looped 2,235 times before anyone noticed.

These imports are the exact ones systemd does at startup — if this test
passes, `python -m robothor.engine.daemon` will at least get past module
load.
"""

import importlib


def test_daemon_imports() -> None:
    importlib.import_module("robothor.engine.daemon")


def test_runner_imports() -> None:
    mod = importlib.import_module("robothor.engine.runner")
    assert hasattr(mod, "AgentRunner"), "runner module must expose AgentRunner"


def test_runner_sibling_modules_import() -> None:
    # These four were clobbered into the non-existent ``robothor.agent.*``
    # namespace in the 2026-04-24 incident. Pinning them here so any
    # future misrouting is caught before systemd is the one finding out.
    importlib.import_module("robothor.engine.config")
    importlib.import_module("robothor.engine.models")
    importlib.import_module("robothor.engine.session")
    importlib.import_module("robothor.engine.tools")
