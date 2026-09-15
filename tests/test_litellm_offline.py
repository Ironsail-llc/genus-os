"""Importing the platform pins LiteLLM to its bundled price list.

LiteLLM 1.101.0 (PyPI 2026-09-14 23:15Z) fetches ``model_prices_and_context_window``
from GitHub when it is imported. main's CI went red at 23:17Z: the bridge
suite refuses network egress, ``manifest_schema`` lazily imports
``llm_client`` (which imports litellm) inside the semantic checks, and the
refusal surfaced as an AssertionError the validator swallowed — fourteen
create tests answered 422. The platform must never depend on an outbound
call it did not make on purpose, so the package root sets the flag before
any module can import litellm.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_importing_the_package_pins_litellm_offline() -> None:
    import robothor  # noqa: F401 - the import is the act under test

    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


def test_a_fresh_interpreter_sees_the_flag_before_anything_else_imports_litellm() -> None:
    code = (
        "import os, sys\n"
        "assert 'litellm' not in sys.modules\n"
        "import robothor\n"
        "print(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip() == "True"


def test_an_operator_choice_is_respected() -> None:
    code = (
        "import os\n"
        "os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'false'\n"
        "import robothor\n"
        "print(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip() == "false"
