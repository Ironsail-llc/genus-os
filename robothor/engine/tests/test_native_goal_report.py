"""Factual goal reports delivered through native chat, with no model rewrite."""

import os
from pathlib import Path

import pytest

from robothor.engine.tests.test_runtime_chat_goal_acceptance import (  # noqa: F401
    db,
    private_database,
    runner,
    runtime_db,
)
from robothor.engine.tests.test_runtime_chat_goal_acceptance import (
    test_unfinished_goal_review_and_pause_through_normal_chat as run_chat_case,
)


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize("reporting", [True, "batch", "steer"])
async def test_factual_goal_status_and_pause_through_native_chat(  # noqa: F811
    request,
    db,  # noqa: F811
    runtime_db,  # noqa: F811
    sample_agent_config,
    monkeypatch,
    tmp_path,  # noqa: F811
    reporting,
):
    if os.environ.get("ROBOTHOR_CHAT_GOAL_UAT_OUTPUT"):
        path = Path(os.environ["ROBOTHOR_CHAT_GOAL_UAT_OUTPUT"])
        monkeypatch.setenv(
            "ROBOTHOR_CHAT_GOAL_UAT_OUTPUT",
            str(
                path.with_stem(
                    path.stem + ("-factual" if reporting is True else f"-factual-{reporting}")
                )
            ),
        )
    await run_chat_case(
        request,
        False,
        db,
        runtime_db,
        sample_agent_config,
        monkeypatch,
        tmp_path,
        reporting=reporting,
    )
