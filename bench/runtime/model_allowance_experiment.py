"""Process-local experiment; never installs a production timeout policy."""


def install(monkeypatch, seconds, *, isolate_short_timeout_health=False):
    from robothor.engine import llm_client

    original_timeout = llm_client._per_call_timeout
    monkeypatch.setattr(
        llm_client,
        "_per_call_timeout",
        lambda model, override: min(seconds, original_timeout(model, override)),
    )
    if isolate_short_timeout_health:
        original_blame = llm_client._blame_model

        def blame(breaker, error, model, attempt_timeout):
            # This is a test allowance, not the provider's ordinary timeout.
            # Other failures still use the native health and alert policy.
            if not isinstance(error, TimeoutError):
                original_blame(breaker, error, model, attempt_timeout)

        monkeypatch.setattr(llm_client, "_blame_model", blame)
