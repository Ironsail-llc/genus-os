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
    compose_metric,
    scope_for,
    trace_query,
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


def test_letters_outside_ascii_are_still_accepted() -> None:
    assert (
        compose(SCOPE, "message:caf\u00e9 \u540d\u524d")
        == f"({SCOPE}) AND (message:caf\u00e9 \u540d\u524d)"
    )


def test_a_metric_tag_may_end_in_a_wildcard(scopes) -> None:
    scopes({"acme-child-a": {"logs": "service:shop-api", "metric_tags": ["service:shop-*"]}})
    scope = scope_for("acme-child-a")
    assert scope is not None and scope.metric_tags == ("service:shop-*",)


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
        # Typographic quotes and brackets: a parser that reads them as phrase
        # or group delimiters would see a different structure from the scan.
        "\u201c(\u201d ) OR * OR ( \u201c)\u201d",
        '"(\u201d ) OR * OR ( \u201c)"',
        "\u2018(\u2019 ) OR * OR ( \u2018)\u2019",
        "\u00ab(\u00bb ) OR * OR ( \u00ab)\u00bb",
        "\u201e(\u201f ) OR * OR ( \u201e)\u201f",
        "\uff02(\uff02 ) OR * OR ( \uff02)\uff02",
        "\uff07a",
        "a \uff09 OR \uff08 b",
        "a \uff3d b",
        "a \u2769 b",
        "a\u3009b",
        "a\uff3cb",
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
    "\u201c",
    "\u201d",
    "\u2018",
    "\u2019",
    "\u00ab",
    "\uff08",
    "\uff09",
    "\uff02",
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


# ── trace ids: a value, never query syntax ──────────────────────────────────


@pytest.mark.parametrize(
    "trace_id",
    ["1234567890123456789", "4bf92f3577b34da6a3ce929d0e0e4736", "ABCDEF0123", "7"],
)
def test_a_hex_or_decimal_trace_id_becomes_a_trace_filter(trace_id) -> None:
    assert trace_query(trace_id) == f"trace_id:{trace_id}"


def test_a_trace_id_is_stripped() -> None:
    assert trace_query("  abc123 ") == "trace_id:abc123"


@pytest.mark.parametrize(
    "trace_id",
    [
        "",
        "   ",
        "abc) OR (service:other",
        "abc OR service:other",
        "abc*",
        'abc"',
        "abc\\",
        "trace_id:abc",
        "12-34",
        "0x1234",
        "g123",
        "1" * 41,
        "abc\n",
    ],
)
def test_a_trace_id_that_is_not_plain_hex_or_digits_is_refused(trace_id) -> None:
    with pytest.raises(ScopeError) as caught:
        trace_query(trace_id)
    assert caught.value.reason


# ── metric queries: built from parts, the scope tags always inside ──────────

METRIC_SCOPE = ("service:shop-api", "env:prod")


def test_a_metric_query_carries_the_scope_tags_then_the_callers() -> None:
    assert (
        compose_metric(METRIC_SCOPE, "trace.http.request.hits", tags=("resource_name:checkout",))
        == "avg:trace.http.request.hits{service:shop-api,env:prod,resource_name:checkout}"
    )


def test_a_metric_query_with_aggregator_group_by_and_count() -> None:
    assert (
        compose_metric(
            METRIC_SCOPE,
            "trace.http.request.errors",
            aggregator="sum",
            group_by=("resource_name", "http.status_code"),
            as_count=True,
        )
        == "sum:trace.http.request.errors{service:shop-api,env:prod}"
        " by {resource_name,http.status_code}.as_count()"
    )


def test_the_every_metric_scope_with_no_tags_is_a_star() -> None:
    assert compose_metric((), "system.cpu.user") == "avg:system.cpu.user{*}"


def test_the_every_metric_scope_takes_the_callers_tags_alone() -> None:
    assert (
        compose_metric((), "system.cpu.user", aggregator="max", tags=("host:web-1",))
        == "max:system.cpu.user{host:web-1}"
    )


@pytest.mark.parametrize("aggregator", ["avg", "sum", "min", "max"])
def test_each_aggregator_is_accepted(aggregator) -> None:
    assert compose_metric(METRIC_SCOPE, "m", aggregator=aggregator).startswith(f"{aggregator}:m{{")


@pytest.mark.parametrize("scope", [(NO_ACCESS_TAG,), ("service:shop-api", NO_ACCESS_TAG)])
def test_a_no_access_metric_scope_refuses(scope) -> None:
    with pytest.raises(ScopeError):
        compose_metric(scope, "system.cpu.user")


@pytest.mark.parametrize("scope", [("service:a}",), ("a b",), ("",), ("x,y",)])
def test_a_malformed_metric_scope_refuses(scope) -> None:
    with pytest.raises(ScopeError):
        compose_metric(scope, "system.cpu.user")


@pytest.mark.parametrize(
    "metric",
    [
        "",
        "1metric",
        "cpu{*}",
        "cpu}",
        "cpu,host:a",
        "cpu user",
        'cpu"',
        "cpu-user",
        "cpu:user",
        "a" * 201,
        "sum:cpu",
        "cpu\n",
    ],
)
def test_an_injection_in_the_metric_name_is_refused(metric) -> None:
    with pytest.raises(ScopeError):
        compose_metric(METRIC_SCOPE, metric)


@pytest.mark.parametrize("aggregator", ["", "count", "avg:x", "AVG ", "p99", "avg}"])
def test_an_unknown_aggregator_is_refused(aggregator) -> None:
    with pytest.raises(ScopeError):
        compose_metric(METRIC_SCOPE, "m", aggregator=aggregator)


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "*",
        "host:a}",
        "host:a},{",
        "host:a,service:other",
        "host:a OR service:other",
        'host:"a"',
        "host:'a'",
        "!host:a",
        "-host:a",
        "(host:a)",
        "host:a\n",
    ],
)
def test_an_injection_in_a_caller_tag_is_refused(tag) -> None:
    with pytest.raises(ScopeError):
        compose_metric(METRIC_SCOPE, "m", tags=(tag,))
    with pytest.raises(ScopeError):
        compose_metric((), "m", tags=("env:prod", tag))


@pytest.mark.parametrize("key", ["", "host}", "host,env", "host env", 'host"', "{host", "*"])
def test_an_injection_in_a_group_by_key_is_refused(key) -> None:
    with pytest.raises(ScopeError):
        compose_metric(METRIC_SCOPE, "m", group_by=(key,))


@pytest.mark.parametrize(("tags", "group_by"), [("env:prod", ()), ((), "host"), ("prod", "host")])
def test_a_bare_string_for_tags_or_group_by_is_refused_not_split(tags, group_by) -> None:
    with pytest.raises(ScopeError) as caught:
        compose_metric(METRIC_SCOPE, "m", tags=tags, group_by=group_by)
    assert "list" in caught.value.reason


_TAG_TOKENS = ("host", ":", "a", "-", "*", ".", "/", "_", "}", "{", ",", " ", '"', "'", "1", "!")


def test_no_generated_metric_tag_escapes_the_scope_braces() -> None:
    """Whatever caller tags are accepted, the braces hold the scope tags first, intact."""
    rng = random.Random(20261126)
    accepted = 0
    for _ in range(5000):
        tags = tuple(
            "".join(rng.choice(_TAG_TOKENS) for _ in range(rng.randint(0, 6)))
            for _ in range(rng.randint(0, 3))
        )
        try:
            out = compose_metric(METRIC_SCOPE, "m", tags=tags)
        except ScopeError:
            continue
        accepted += 1
        assert out.startswith("avg:m{") and out.endswith("}")
        inner = out[len("avg:m{") : -1]
        assert "{" not in inner and "}" not in inner and " " not in inner
        parts = inner.split(",")
        assert tuple(parts[: len(METRIC_SCOPE)]) == METRIC_SCOPE
        assert tuple(parts[len(METRIC_SCOPE) :]) == tags
    assert accepted > 200
