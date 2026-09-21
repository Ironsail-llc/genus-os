"""Present terminal run failures honestly, including after streamed partial text."""

from robothor.engine.models import RunStatus


def result_text(run):
    if run.status in {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}:
        reason = run.error_message or "Completion was not verified."
        return f"[Run {run.status.value}: {reason}]"
    return run.output_text or ""


def receipt_result_text(run, receipts):
    """Present persisted action evidence without upgrading overall completion."""
    from robothor.engine.chat_receipts import receipt_summary

    text = result_text(run)
    if not receipts:
        return text
    evidence = receipt_summary(receipts)
    if run.status in {RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}:
        return evidence + "\n\nExecution was interrupted. Any remaining work is not confirmed."
    incomplete = any(
        not item["verified"] and item["status"] != "draft" and not item.get("superseded_by")
        for item in receipts
    )
    if run.status == RunStatus.COMPLETED and incomplete:
        text = "The run ended, but a recorded action is not fully verified."
    return "\n\n".join(filter(None, [text, evidence]))
