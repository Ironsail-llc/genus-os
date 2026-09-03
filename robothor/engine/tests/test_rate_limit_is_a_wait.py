"""A rate limit is a wait. A spent budget is not.

`_handle_model_error` grouped 429 with 401/402/403 — auth failures — and
marked the model BROKEN for the rest of the run. But a 429 is the provider
saying "not right now", and the fix for that is to wait the interval it
names, not to burn the primary model and fall down the chain for every
subsequent call.

Three distinct conditions were collapsed into one, and a competitive audit
of four agent harnesses found this the clearest single gap against the
strongest of them (Hermes carries a ~25-member failure taxonomy; this was
one crude class):

* **429 with Retry-After** — wait that long, then retry the SAME model.
* **429 without it** — back off and retry the same model a bounded number
  of times.
* **402 / insufficient credit** — the account is out of money. No amount of
  waiting fixes it and no other model on the same key will work either.
  This one cost a real campaign: a spent key produced task after task of
  zeros that looked exactly like capability failures.

`Retry-After` is honoured but never trusted blindly — a provider can name an
hour, and a run that sleeps an hour inside its own wall-clock ceiling has
simply thrown the budget away in a different manner.
"""

from __future__ import annotations

from robothor.engine.llm_client import (
    MAX_RATE_LIMIT_WAIT,
    is_credit_exhausted,
    is_periodic_quota_exhausted,
    rate_limit_wait_seconds,
)


class _Resp:
    def __init__(self, headers):
        self.headers = headers


class _Err(Exception):  # noqa: N818 - a test double, not a real error type
    def __init__(self, msg="", status=None, headers=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if headers is not None:
            self.response = _Resp(headers)


class TestReadingRetryAfter:
    def test_seconds_form(self):
        assert rate_limit_wait_seconds(_Err(status=429, headers={"Retry-After": "12"})) == 12.0

    def test_header_name_is_case_insensitive(self):
        assert rate_limit_wait_seconds(_Err(status=429, headers={"retry-after": "7"})) == 7.0

    def test_x_ratelimit_reset_is_also_read(self):
        e = _Err(status=429, headers={"x-ratelimit-reset-requests": "3"})
        assert rate_limit_wait_seconds(e) == 3.0

    def test_a_wait_longer_than_the_cap_is_clamped(self):
        """A provider naming an hour must not eat a run's whole budget."""
        got = rate_limit_wait_seconds(_Err(status=429, headers={"Retry-After": "3600"}))
        assert got == MAX_RATE_LIMIT_WAIT

    def test_the_cap_is_short_enough_to_be_worth_waiting(self):
        assert 5 <= MAX_RATE_LIMIT_WAIT <= 60

    def test_a_garbage_header_falls_back_rather_than_raising(self):
        assert rate_limit_wait_seconds(_Err(status=429, headers={"Retry-After": "soon"})) > 0

    def test_no_header_still_yields_a_backoff(self):
        assert rate_limit_wait_seconds(_Err(status=429, headers={})) > 0

    def test_a_non_rate_limit_error_yields_nothing(self):
        assert rate_limit_wait_seconds(_Err(status=500, headers={})) is None
        assert rate_limit_wait_seconds(_Err("boom")) is None


class TestCreditExhaustion:
    def test_402_is_credit_exhaustion(self):
        assert is_credit_exhausted(_Err(status=402))

    def test_the_message_form_is_recognised(self):
        for msg in (
            "Key limit exceeded",
            "insufficient credits for this request",
            "You exceeded your current quota",
            "billing hard limit has been reached",
        ):
            assert is_credit_exhausted(_Err(msg)), msg

    def test_an_ordinary_rate_limit_is_not_exhaustion(self):
        assert not is_credit_exhausted(_Err("rate limit exceeded", status=429))

    def test_an_unrelated_error_is_not(self):
        assert not is_credit_exhausted(_Err("connection reset", status=500))


class TestTheDispatcherUsesThem:
    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import robothor.engine.llm_client as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_429_is_no_longer_grouped_with_auth_failures(self):
        body = self._source()
        assert "status in (401, 402, 403, 429, 500, 502, 503, 504)" not in body, (
            "429 still marks the model broken — a rate limit is a wait, not a dead model"
        )

    def _retry_block(self) -> str:
        """The dispatcher's except-block, not the whole file. A grep over the
        file passes on the helper's own DEFINITION — the weak-wiring-test
        trap this codebase keeps falling into."""
        body = self._source()
        start = body.index("except Exception as e:\n                    last_error = e")
        return body[start : body.index("# Giving up on this model", start)]

    def test_the_retry_path_consults_the_wait(self):
        """The except-block hands rate limits to ``_wait_out_rate_limit``,
        and THAT helper is the one that computes the wait and sleeps it.
        Checking the helper's own body (not the whole file) keeps the
        definition of ``rate_limit_wait_seconds`` from satisfying the test."""
        import inspect

        from robothor.engine.llm_client import LLMClient

        block = self._retry_block()
        assert "_wait_out_rate_limit(" in block, "the retry path never consults the wait"
        helper = inspect.getsource(LLMClient._wait_out_rate_limit)
        assert "rate_limit_wait_seconds(" in helper, "the wait is computed but never used"
        assert "await asyncio.sleep(" in helper, "it computes a wait and does not wait"

    def test_credit_exhaustion_short_circuits_the_chain(self):
        """Walking the fallback chain on a spent key wastes every model on it."""
        block = self._retry_block()
        assert "is_credit_exhausted(" in block
        assert "raise" in block, "exhaustion must stop the chain, not fall through"


class TestStatusBeatsProse:
    """An existing runner test raises 403 with the message "Rate limited".
    A 403 is Forbidden however it is worded; treating it as a wait would
    retry an auth failure instead of falling to the next model."""

    def test_403_saying_rate_limited_is_not_a_wait(self):
        assert rate_limit_wait_seconds(_Err("Rate limited", status=403)) is None

    def test_401_saying_rate_limited_is_not_a_wait(self):
        assert rate_limit_wait_seconds(_Err("rate limit exceeded", status=401)) is None

    def test_a_statusless_rate_limit_still_waits(self):
        """Some providers raise bare exceptions; then prose is all there is."""
        assert rate_limit_wait_seconds(_Err("rate limit exceeded")) > 0


class TestPeriodicQuotaIsNotASpendCap:
    """A calendar cap and a spent balance recover on different clocks.

    2026-08-27: "Key limit exceeded (weekly limit)" matched the credit
    markers, so the pool retried it on the 900s spend-cap cooldown — ~96
    revivals a day, each one a fresh burst of 403s through every agent's
    chain. Both are still "the account cannot pay", so is_credit_exhausted
    must keep returning True; only the RETIREMENT DURATION differs.
    """

    def test_weekly_limit_prose_classifies_as_periodic(self):
        assert is_periodic_quota_exhausted(_Err("Key limit exceeded (weekly limit)"))

    def test_daily_and_monthly_windows_too(self):
        for msg in ("Key limit exceeded (daily limit)", "monthly limit reached"):
            assert is_periodic_quota_exhausted(_Err(msg)), msg

    def test_a_plain_spend_cap_is_not_periodic(self):
        for msg in ("insufficient credit", "not enough credits", "payment required"):
            assert not is_periodic_quota_exhausted(_Err(msg)), msg

    def test_a_bare_402_is_not_periodic(self):
        assert not is_periodic_quota_exhausted(_Err(status=402))

    def test_a_periodic_cap_is_still_credit_exhausted(self):
        """The chain must still stop dialling; only the cooldown changes."""
        assert is_credit_exhausted(_Err("Key limit exceeded (weekly limit)"))

    def test_a_rate_limit_is_neither(self):
        assert not is_periodic_quota_exhausted(_Err("rate limit exceeded", status=429))
