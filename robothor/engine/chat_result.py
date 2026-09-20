"""Present terminal run failures honestly, including after streamed partial text."""

from robothor.engine.models import RunStatus


def result_text(run):
    if run.status in {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}:
        reason = run.error_message or "Completion was not verified."
        return f"[Run {run.status.value}: {reason}]"
    return run.output_text or ""
