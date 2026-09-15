"""``GET /api/audit/events.csv`` — the audit log, downloadable.

An export is read by two things that ``/api/audit/events`` is not: a
spreadsheet, and a person who will believe what the spreadsheet shows them. Both
create failure modes that JSON does not have, and each is a test here:

* **A cell that a spreadsheet EXECUTES.** ``=HYPERLINK("http://evil","ok")`` in
  an audit detail is a formula the moment the file is opened in Excel, Numbers
  or Sheets — and the detail is attacker-influenced content by construction (it
  is what the appliance logged about somebody's request). Any cell opening with
  ``= + - @``, a tab or a CR is prefixed with an apostrophe, which is the one
  neutralisation every spreadsheet honours.
* **An empty file that looks like an export.** ``/events`` swallows every
  failure into a 200 with ``{"error": ...}``; a CSV route that did the same
  would hand the operator a zero-row audit trail and no reason to doubt it. This
  route returns a real 500.

Plus the two the JSON route also has to get right, restated because the CSV
path builds its own rows: the gate (operator or auditor, never an agent), and
log injection (every cell through ``sanitize_log``, so a newline in a detail
cannot forge a second row).
"""

from __future__ import annotations

import csv
import io

import pytest

CSV = "/api/audit/events.csv"


def _events(monkeypatch, rows):
    """Stand in for ``robothor.audit.logger.query_log`` at the router's seam.

    Patched as the ROUTER's name, not the logger's: the point of each test
    below is what this module does with the rows it is handed, and a fake
    bound at the router is the only version of that which needs no database.
    """
    captured: dict = {}

    def _query_log(**kwargs):
        captured.update(kwargs)
        return rows

    monkeypatch.setattr("routers.audit.query_log", _query_log)
    return captured


def _event(**overrides):
    base = {
        "id": 1,
        "timestamp": "2026-09-15T12:00:00+00:00",
        "event_type": "user.invite",
        "category": "bridge",
        "actor": "operator:alice",
        "action": "account-1",
        "details": {"role": "member"},
        "source_channel": "helm",
        "target": None,
        "status": "ok",
        "session_key": None,
        "user_id": "",
    }
    base.update(overrides)
    return base


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ── the gate ─────────────────────────────────────────────────────────────────


def test_an_auditor_may_export(controls_client_as_auditor, monkeypatch):
    _events(monkeypatch, [_event()])
    resp = controls_client_as_auditor.get(CSV)
    assert resp.status_code == 200


def test_an_operator_may_export(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event()])
    assert controls_client_as_operator.get(CSV).status_code == 200


def test_a_service_token_may_not_export(controls_client_as_service, monkeypatch):
    """An agent with ``audit:read`` passes the middleware; the router still refuses."""
    _events(monkeypatch, [_event()])
    assert controls_client_as_service.get(CSV).status_code == 403


def test_a_member_may_not_export(controls_client_as_user, monkeypatch):
    _events(monkeypatch, [_event()])
    assert controls_client_as_user.get(CSV).status_code == 403


# ── the file ─────────────────────────────────────────────────────────────────


def test_it_is_served_as_a_named_csv_attachment(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event()])
    resp = controls_client_as_operator.get(CSV)
    assert resp.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in resp.headers["content-type"]
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="audit-')
    assert disposition.endswith('.csv"')


def test_the_first_row_is_a_header(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event()])
    rows = _rows(controls_client_as_operator.get(CSV).text)
    assert rows[0][0] == "id"
    assert "event_type" in rows[0] and "details" in rows[0]
    assert len(rows) == 2


def test_a_nested_detail_is_json_in_one_cell(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event(details={"role": "member", "nested": {"a": 1}})])
    rows = _rows(controls_client_as_operator.get(CSV).text)
    cell = rows[1][rows[0].index("details")]
    assert cell.startswith("{") and '"nested"' in cell


@pytest.mark.parametrize(
    "payload",
    [
        '=HYPERLINK("http://example.invalid","click")',
        "+1+1",
        "-2+3",
        "@SUM(A1:A9)",
        "\tTAB",
    ],
)
def test_a_formula_cell_is_neutralised(controls_client_as_operator, monkeypatch, payload):
    _events(monkeypatch, [_event(action=payload, details={"note": payload})])
    rows = _rows(controls_client_as_operator.get(CSV).text)
    for cell in rows[1]:
        assert cell[:1] not in {"=", "+", "-", "@", "\t", "\r"}, cell
    assert rows[1][rows[0].index("action")].startswith("'")


def test_a_credential_shaped_detail_is_redacted(controls_client_as_operator, monkeypatch):
    """``_audit.audited`` says identifiers only, and the routes that use it obey.

    ``log_event`` has callers all over the platform, though, and this is the
    route whose output LEAVES the appliance — into a downloads folder, an
    email, a ticket. So the same redactor the log routes use runs here too,
    even though the JSON ``/events`` route does not have it.
    """
    _events(
        monkeypatch,
        [_event(details={"note": "retry with OPENROUTER_API_KEY=sk-or-abc123def456ghi789"})],
    )
    text = controls_client_as_operator.get(CSV).text
    assert "sk-or-abc123def456ghi789" not in text
    assert "OPENROUTER_API_KEY" in text


def test_a_newline_in_a_detail_cannot_forge_a_row(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event(action="one\r\n9,forged,row,,,,,,,,,")])
    rows = _rows(controls_client_as_operator.get(CSV).text)
    assert len(rows) == 2, "sanitize_log must escape the record separators"
    assert "forged" in rows[1][rows[0].index("action")]


def test_a_comma_and_a_quote_are_rfc4180_quoted(controls_client_as_operator, monkeypatch):
    _events(monkeypatch, [_event(action='a,b "c"')])
    text = controls_client_as_operator.get(CSV).text
    assert '"a,b ""c"""' in text
    assert _rows(text)[1][5] == 'a,b "c"'


# ── the filters ──────────────────────────────────────────────────────────────


def test_it_takes_the_same_filters_as_the_json_route(controls_client_as_operator, monkeypatch):
    captured = _events(monkeypatch, [])
    controls_client_as_operator.get(
        f"{CSV}?since=2026-09-01&event_type=user.invite&actor=operator:alice&user_id=u1&limit=4000"
    )
    assert captured["since"] == "2026-09-01T00:00:00", "parsed and re-emitted, not forwarded"
    assert captured["event_type"] == "user.invite"
    assert captured["actor"] == "operator:alice"
    assert captured["user_id"] == "u1"
    assert captured["limit"] == 4000


def test_until_is_applied(controls_client_as_operator, monkeypatch):
    _events(
        monkeypatch,
        [
            _event(id=1, timestamp="2026-09-01T00:00:00+00:00"),
            _event(id=2, timestamp="2026-09-20T00:00:00+00:00"),
        ],
    )
    rows = _rows(controls_client_as_operator.get(f"{CSV}?until=2026-09-10").text)
    assert [r[0] for r in rows[1:]] == ["1"]


@pytest.mark.parametrize("limit", ["0", "5001", "abc", "-1", "1_0", "+7", " 5 ", "007"])
def test_limit_is_bounded_at_five_thousand(controls_client_as_operator, monkeypatch, limit):
    _events(monkeypatch, [])
    resp = controls_client_as_operator.get(f"{CSV}?limit={limit}")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str), (
        "the Helm was promised one 422 body shape across all four routes; a "
        "pydantic Field cap renders the nested one"
    )


@pytest.mark.parametrize("field", ["since", "until"])
@pytest.mark.parametrize(
    "value",
    [
        "notatimestamp",
        "yesterday",
        "2026-13",
        "'; --",
        "2026-09-01 ",
        # Four-two-two digits, and not a month. The PATTERN admits it; only
        # parsing knows it is not a date, and unparsed it reached psycopg2.
        "2026-13-01",
        "2026-02-30",
        "2026-09-01T25:00:00",
    ],
)
def test_a_malformed_time_bound_is_a_422_not_a_500(
    controls_client_as_operator, monkeypatch, field, value
):
    """A caller's typo is not an appliance fault.

    Unvalidated, these reached psycopg2's timestamp comparison and came back as
    ``500 {"error": "internal error"}`` — in a route whose whole argument for
    returning a real 500 is that a 500 means "we broke".
    """
    _events(monkeypatch, [])
    resp = controls_client_as_operator.get(f"{CSV}?{field}={value}")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("2026-09-01", "2026-09-01T00:00:00"),
        ("2026-09-01T10:00", "2026-09-01T10:00:00"),
        ("2026-09-01T10:00:00", "2026-09-01T10:00:00"),
        ("2026-09-01T10:00:00Z", "2026-09-01T10:00:00+00:00"),
    ],
)
def test_a_real_timestamp_is_parsed_and_re_emitted(
    controls_client_as_operator, monkeypatch, value, canonical
):
    """Accepted, and normalised on the way through.

    The same parse that turns ``2026-13-01`` into a 422 spells the survivors
    Python's way. The audit store gets one shape whatever the caller typed.
    """
    captured = _events(monkeypatch, [])
    assert controls_client_as_operator.get(f"{CSV}?since={value}").status_code == 200
    assert captured["since"] == canonical


# ── failure ──────────────────────────────────────────────────────────────────


def test_a_failed_query_is_a_500_not_an_empty_export(controls_client_as_operator, monkeypatch):
    """An empty file that looks like an export is worse than an error."""

    def _boom(**_kwargs):
        raise RuntimeError("audit store is down")

    monkeypatch.setattr("routers.audit.query_log", _boom)
    resp = controls_client_as_operator.get(CSV)
    assert resp.status_code == 500
    assert "audit store is down" not in resp.text


def test_the_json_route_still_swallows_its_errors(controls_client_as_operator, monkeypatch):
    """Deliberately unchanged: the Helm's Audit page reads the 200 shape."""

    def _boom(**_kwargs):
        raise RuntimeError("audit store is down")

    monkeypatch.setattr("routers.audit.query_log", _boom)
    resp = controls_client_as_operator.get("/api/audit/events")
    assert resp.status_code == 200
    assert resp.json()["error"] == "internal error"
