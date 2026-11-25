"""The tenant wall: which filter bounds a tenant, and a caller query that cannot widen it.

Every Datadog query is ``(tenant scope) AND (caller query)``. These tests are
the reason a caller cannot read another tenant's logs: a query that could end
the wrapping parenthesis early, open a phrase that swallows it, or escape it
with a backslash is refused before anything is sent.
"""

from __future__ import annotations

import json
import logging
import random

import pytest

from robothor.integrations.datadog.scope import (
    MAX_QUERY_CHARS,
    NO_ACCESS_QUERY,
    NO_ACCESS_TAG,
    DatadogScope,
    ScopeError,
    compose,
    scope_for,
)

SCOPE = "service:shop-api"


@pytest.fixture
def scopes(monkeypatch):
    """Declare ``ROBOTHOR_DATADOG_SCOPES`` for one test."""
    from robothor.settings import reset_settings

    def _set(value: object) -> None:
        raw = value if isinstance(value, str) else json.dumps(value)
        monkeypatch.setenv("ROBOTHOR_DATADOG_SCOPES", raw)
        reset_settings()

    yield _set
    reset_settings()


# ── scope_for ────────────────────────────────────────────────────────────────


def test_a_full_entry_is_read_as_declared(scopes) -> None:
    scopes(
        {
            "acme-child-b": {
                "logs": "service:clinic-app",
                "spans": "service:clinic-app env:prod",
                "monitors": "team:clinic",
                "metric_tags": ["service:clinic-app", "env:prod"],
            }
        }
    )
    assert scope_for("acme-child-b") == DatadogScope(
        tenant_id="acme-child-b",
        logs="service:clinic-app",
        spans="service:clinic-app env:prod",
        monitors="team:clinic",
        metric_tags=("service:clinic-app", "env:prod"),
    )


def test_spans_default_to_logs_and_the_rest_to_no_access(scopes) -> None:
    scopes({"acme-child-a": {"logs": "service:shop-api"}})
    scope = scope_for("acme-child-a")
    assert scope is not None
    assert scope.spans == "service:shop-api"
    assert scope.monitors == NO_ACCESS_QUERY
    # "No access" must fail closed even for a caller that ANDs the tags in
    # without looking: () would mean everything.
    assert scope.metric_tags == (NO_ACCESS_TAG,)


def test_an_explicit_empty_tag_list_means_every_metric(scopes) -> None:
    scopes({"acme-parent": {"logs": "*", "metric_tags": []}})
    scope = scope_for("acme-parent")
    assert scope is not None and scope.metric_tags == ()


def test_a_tenant_without_an_entry_is_not_configured(scopes) -> None:
    scopes({"acme-child-a": {"logs": "service:shop-api"}})
    assert scope_for("acme-child-b") is None


def test_nothing_declared_is_not_configured(scopes) -> None:
    scopes("")
    assert scope_for("acme-child-a") is None


@pytest.mark.parametrize("logs", ["", "   ", None, 7, ["service:shop-api"]])
def test_a_blank_or_non_string_logs_filter_is_not_everything(scopes, logs) -> None:
    scopes({"acme-child-a": {"logs": logs}})
    assert scope_for("acme-child-a") is None


def test_only_the_literal_star_means_everything(scopes) -> None:
    scopes({"acme-parent": {"logs": "*"}})
    scope = scope_for("acme-parent")
    assert scope is not None and scope.logs == "*"


@pytest.mark.parametrize(
    "entry",
    [
        "service:shop-api",
        ["service:shop-api"],
        {"logs": "service:shop-api) OR (*"},
        {"logs": 'service:"shop-api'},
        {"logs": "service:shop-api", "spans": 3},
        {"logs": "service:shop-api", "monitors": "team:a) OR (team:b"},
        {"logs": "service:shop-api", "metric_tags": "service:shop-api"},
        {"logs": "service:shop-api", "metric_tags": ["service:shop-api,env:prod"]},
        {"logs": "service:shop-api", "metric_tags": ["service:{shop}"]},
        {"logs": "service:shop-api", "metric_tags": [""]},
        {"logs": "service:shop-api", "metric_tags": [7]},
    ],
)
def test_a_malformed_entry_is_not_configured(scopes, entry) -> None:
    scopes({"acme-child-a": entry})
    assert scope_for("acme-child-a") is None


def test_malformed_json_is_not_configured_and_warns_once_without_the_content(
    scopes, caplog
) -> None:
    scopes('{"acme-child-a": {"logs": "service:private-marker"')
    with caplog.at_level(logging.WARNING, logger="robothor.integrations.datadog.scope"):
        assert scope_for("acme-child-a") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "private-marker" not in warnings[0].getMessage()


def test_a_malformed_entry_warns_without_the_content(scopes, caplog) -> None:
    scopes({"acme-child-a": {"logs": "service:private-marker) OR (*"}})
    with caplog.at_level(logging.WARNING, logger="robothor.integrations.datadog.scope"):
        assert scope_for("acme-child-a") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "private-marker" not in warnings[0].getMessage()


def test_a_json_value_that_is_not_an_object_is_not_configured(scopes) -> None:
    scopes('["acme-child-a"]')
    assert scope_for("acme-child-a") is None


def test_one_bad_entry_does_not_unconfigure_the_others(scopes) -> None:
    scopes({"acme-child-a": {"logs": ""}, "acme-child-b": {"logs": "service:clinic-app"}})
    assert scope_for("acme-child-a") is None
    scope = scope_for("acme-child-b")
    assert scope is not None and scope.logs == "service:clinic-app"


# ── compose: the shape of an accepted query ─────────────────────────────────


def test_the_caller_query_is_anded_inside_its_own_parentheses() -> None:
    assert compose(SCOPE, "status:error") == "(service:shop-api) AND (status:error)"


def test_the_caller_query_is_stripped() -> None:
    assert compose(SCOPE, "  status:error \t") == "(service:shop-api) AND (status:error)"


@pytest.mark.parametrize("user", ["", "   ", "\t"])
def test_an_empty_caller_query_is_the_scope_alone(user) -> None:
    assert compose(SCOPE, user) == SCOPE


def test_the_everything_scope_passes_the_caller_query_through() -> None:
    assert compose("*", "status:error") == "status:error"
    assert compose("*", "") == "*"


@pytest.mark.parametrize("scope", ["", "   ", NO_ACCESS_QUERY])
def test_a_scope_with_no_access_refuses(scope) -> None:
    with pytest.raises(ScopeError):
        compose(scope, "status:error")


# ── compose: refusals ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user",
    [
        ") OR (*",
        "*) OR (service:other",
        "foo) AND (bar",
        "a ] b",
        "(a",
        "a)",
        "[a TO b)",
        "{a TO b]",
        "(a]",
        '"unclosed',
        '\\"',
        '"a\\"',  # the escaped closing quote leaves the phrase open
        '"a \\" ) OR ( b"',  # a backslash inside a phrase reads two ways
        "foo\\",  # would escape the closing parenthesis
        "foo\\)",
        "foo\\(",
        "a\\]",
        "a\\\\",
        "a\nb",
        "a\rb",
        "a\x00b",
        "a\x1bb",
        "a b",
        "a‮b",
        "message:don't",  # read as a phrase opener by a parser that honours single quotes
        "'(' ) OR ( ')'",
    ],
)
def test_a_query_that_could_change_the_wrapping_is_refused(user) -> None:
    with pytest.raises(ScopeError) as caught:
        compose(SCOPE, user)
    reason = caught.value.reason
    assert reason and len(reason) < 120 and "\n" not in reason
    assert str(caught.value) == reason


def test_a_query_over_the_malformed_input_guard_is_refused() -> None:
    with pytest.raises(ScopeError):
        compose(SCOPE, "a" * (MAX_QUERY_CHARS + 1))
    assert compose(SCOPE, "a" * MAX_QUERY_CHARS).endswith(")")


def test_the_everything_scope_still_refuses_a_malformed_query() -> None:
    with pytest.raises(ScopeError):
        compose("*", ") OR (*")


def test_a_malformed_scope_refuses() -> None:
    with pytest.raises(ScopeError):
        compose("service:a) OR (service:b", "status:error")


def test_scope_error_is_a_value_error() -> None:
    assert issubclass(ScopeError, ValueError)


# ── compose: legitimate queries ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "user",
    [
        'message:")"',
        '"a ( b ] c"',
        "(service:shop-api OR service:clinic-app) AND -(status:info OR status:debug)",
        "@duration:[100 TO 500]",
        "@http.status_code:[400 TO 499] -status:ok",
        "NOT host:web-1 AND status:error",
        "host:web-\\*",
        "@path:\\/api\\/v1",
        "service:shop* @http.method:POST",
        '"don\'t panic"',
        "{1 TO 5}",
    ],
)
def test_a_legitimate_query_is_accepted_verbatim(user) -> None:
    assert compose(SCOPE, user) == f"({SCOPE}) AND ({user})"


# ── compose: property ───────────────────────────────────────────────────────

_TOKENS = (
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    '"',
    "'",
    "\\",
    " AND ",
    " OR ",
    " NOT ",
    "-",
    "*",
    ":",
    "service",
    "foo",
    "@a.b",
    " TO ",
    " ",
    "\n",
    "\t",
)
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {v: k for k, v in _OPEN.items()}


def _structure(text: str, *, quotes: str, backslash_escapes: bool) -> list[tuple[int, str]]:
    """The structural brackets of ``text`` as one reading of the syntax sees them.

    Independent of the implementation: it never refuses, it reports where the
    brackets are, so the test can ask whether the wrapper's parentheses still
    pair with each other. ``quotes`` are the characters this reading treats as
    phrase delimiters; ``backslash_escapes`` says whether a backslash makes the
    next character literal.
    """
    out: list[tuple[int, str]] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and backslash_escapes:
            i += 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in quotes:
            quote = ch
        elif ch in _OPEN or ch in _CLOSE:
            out.append((i, ch))
        i += 1
    return out


def _partner(text: str, index: int, **reading) -> int | None:
    """The index of the bracket that closes the one at ``index``, under ``reading``."""
    depth = 0
    started = False
    for i, ch in _structure(text, **reading):
        if i < index:
            continue
        if ch in _OPEN:
            depth += 1
            started = True
        else:
            depth -= 1
        if started and depth == 0:
            return i
    return None


_READINGS = [
    {"quotes": '"', "backslash_escapes": True},
    {"quotes": '"', "backslash_escapes": False},
    {"quotes": "\"'", "backslash_escapes": True},
    {"quotes": "\"'", "backslash_escapes": False},
]


def _balanced(user: str) -> bool:
    for reading in _READINGS:
        stack: list[str] = []
        for _i, ch in _structure(user, **reading):
            if ch in _OPEN:
                stack.append(ch)
            elif not stack or stack.pop() != _CLOSE[ch]:
                return False
        if stack:
            return False
    return True


def test_no_generated_query_escapes_the_wrapper() -> None:
    rng = random.Random(20261125)
    accepted = 0
    for _ in range(20000):
        user = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 14)))
        try:
            out = compose(SCOPE, user)
        except ScopeError:
            continue
        accepted += 1
        stripped = user.strip()
        if not stripped:
            assert out == SCOPE
            continue
        prefix = f"({SCOPE}) AND ("
        assert out.startswith(prefix) and out.endswith(")")
        assert out[len(prefix) : -1] == stripped
        assert _balanced(stripped), repr(stripped)
        for reading in _READINGS:
            # The scope's parentheses pair with each other, and so do the
            # caller's wrapper -- under every reading of quotes and escapes.
            assert _partner(out, 0, **reading) == len(SCOPE) + 1, (repr(stripped), reading)
            assert _partner(out, len(prefix) - 1, **reading) == len(out) - 1, (
                repr(stripped),
                reading,
            )
    # Not a vacuous pass: a meaningful share of the generated queries is accepted.
    assert accepted > 1000


def test_generated_balanced_queries_are_accepted() -> None:
    """The guard is not so strict that ordinary nesting is refused."""
    rng = random.Random(7)
    words = ["service:shop-api", "status:error", "@a.b:1", "host:web-*", '"a ) b"']

    def gen(depth: int) -> str:
        if depth == 0 or rng.random() < 0.3:
            return rng.choice(words)
        op = rng.choice([" AND ", " OR ", " "])
        neg = rng.choice(["", "-", "NOT "])
        return f"{neg}({gen(depth - 1)}{op}{gen(depth - 1)})"

    for _ in range(500):
        user = gen(4)
        assert compose(SCOPE, user) == f"({SCOPE}) AND ({user})"
