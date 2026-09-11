"""``write_file`` must always say why it failed.

2026-09-11 15:08: ``write_file`` had 5 failures in the same hour whose
``agent_tool_events.error_message`` was the empty string — the degradation
alert for it could not say what broke. The handler's own exception path
(``robothor/engine/tools/handlers/filesystem.py:265``) always returns a
non-empty ``"Failed to write file: ..."`` message; these tests pin that for
the failure shapes the handler can actually hit. The empty-message rows
turned out to come from a different place — the guardrail-block path in
``tool_admission.py`` not forwarding ``GuardrailResult.reason`` — covered by
``test_tool_admission.py::test_a_block_logs_the_refusal_reason_on_the_tool_event``
and by ``log_tool_event``'s own fallback in ``test_tool_event_reason.py``.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import _write_file


@pytest.mark.asyncio
async def test_writing_over_an_existing_directory_returns_a_non_empty_error(tmp_path):
    """``path.write_text()`` on a directory raises IsADirectoryError — the
    handler must still hand back a reason, not an empty string."""
    target_dir = tmp_path / "a_directory"
    target_dir.mkdir()

    result = await _write_file(
        {"path": str(target_dir), "content": "hello"}, ToolContext(workspace=str(tmp_path))
    )

    assert "error" in result
    assert result["error"].strip(), "an empty error tells the operator nothing"


@pytest.mark.asyncio
async def test_a_parent_path_that_is_a_file_returns_a_non_empty_error(tmp_path):
    """``mkdir(parents=True)`` fails when a path component already exists as
    a plain file — also must not come back with an empty reason."""
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("i am a file")

    result = await _write_file(
        {"path": str(blocking_file / "child.txt"), "content": "hello"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert "error" in result
    assert result["error"].strip(), "an empty error tells the operator nothing"


@pytest.mark.asyncio
async def test_a_successful_write_reports_no_error(tmp_path):
    result = await _write_file(
        {"path": str(tmp_path / "ok.txt"), "content": "hello"},
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.get("success") is True
    assert "error" not in result
