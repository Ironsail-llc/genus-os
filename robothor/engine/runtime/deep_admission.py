"""Check durable controls on the worker thread before starting deep execution."""


def execute_deep_checked(*, run, workspace, **kwargs):
    from robothor.engine.rlm_tool import DeepReasonConfig, execute_deep_reason
    from robothor.engine.runtime.controls import stopped
    from robothor.engine.runtime.provider_budget import DurableStopError

    if stopped(run.tenant_id, run.id):
        raise DurableStopError("Durable stop denies deep execution")
    return execute_deep_reason(config=DeepReasonConfig(workspace=workspace), **kwargs)


def record_deep(run, create_run):
    import logging

    try:
        create_run(run)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Deep admission audit failed (%s)", type(error).__name__
        )
        return "Deep execution could not be recorded; no work was started."
    return None
