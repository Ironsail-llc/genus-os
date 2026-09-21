"""A lost response is not evidence that a potentially mutating call failed."""


def response_failure(error, *, read_only):
    """Preserve read retry behavior; uncertain effects need reconciliation first."""
    if read_only:
        return error
    return {**error, "retryable": False, "outcome_unknown": True}


def tool_response_failure(name, error):
    """A handler may have performed earlier writes before any transport failure."""
    from robothor.engine.tools.read_only import declared_read_only_tools

    return response_failure(error, read_only=name in declared_read_only_tools())


def http_status_failure(name, status, message):
    error = {"error": message, "retryable": status >= 500}
    return tool_response_failure(name, error) if status >= 500 else error
