"""Ingest the weekly HR "Claude Usage Report" PDF for the devops report.

HR emails a hand-made PDF each week with one table row per developer
account: plan, weekly usage %, current-session %, sometimes a separate Fable
column, the weekly reset time and a status note. ``pdftotext -layout`` keeps
the columns aligned, but cells wrap across lines ("Max\\n20x",
"⚠️ 79% — In\\nprogress", "Name –\\nAcct 1") and the column set changes from
week to week, so the parser reads the column positions from the header row
on every page and assigns each text fragment to the column it sits under.

If that deterministic parse is not a consistent table, an optional single
LLM extraction (``DEVOPS_USAGE_LLM_MODEL``) is tried; its cells pass through
the same derivation and validation. Otherwise usage is reported unavailable
with the reason. Labels are always derived in code:

* ``reading``: ``post_reset`` | ``mid_cycle`` | ``unverified`` | ``completed``
* ``limit_status`` (weekly plan limit only): ``hit`` | ``projected`` | ``near`` | ``ok``
* ``fable_limit_hit``: the separate Fable limit — never folded into the above.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SUBJECT_HINT = "Claude Usage Report"
LOOKBACK_DAYS = 10

_COLS = ("developer", "plan", "weekly", "session", "fable", "reset", "note")
_PCT = re.compile(r"(\d{1,4})\s*%")
_DATE = re.compile(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})\b")
_PERIOD = re.compile(r"Week of ([A-Z][a-z]+ \d{1,2}\s*[–-]\s*(?:[A-Z][a-z]+ )?\d{1,2}, \d{4})")
_DEV_ACCT = re.compile(r"^(?P<name>.+?)\s*[–—-]\s*Acct\.?\s*(?P<n>\d+)\s*$", re.IGNORECASE)
_PAGE = re.compile(r"Page \d+ of \d+")
_TOL = 2


class UsageParseError(ValueError):
    pass


# ── Header / column detection ──────────────────────────────────────────


def _header_columns(line: str) -> dict[str, int] | None:
    """Column start offsets from a header row, or None if this is not one."""
    if "Developer" not in line or "Plan" not in line or "Weekly" not in line:
        return None
    cols: dict[str, int] = {"developer": line.index("Developer"), "plan": line.index("Plan")}
    weekly = [m.start() for m in re.finditer(r"Weekly", line)]
    cols["weekly"] = weekly[0]
    cur = line.find("Current")
    if cur < 0:
        return None
    cols["session"] = cur
    fable = line.find("Fable")
    if fable > 0:
        cols["fable"] = fable
    later = [w for w in weekly if w > cur]
    if later:
        cols["reset"] = later[0]
    status = line.find("Status")
    if status < 0:
        status = line.find("Note")
    if status > 0:
        cols["note"] = status
    return cols


def _chunks(line: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]


def _assign(cols: dict[str, int], start: int) -> str:
    best = "developer"
    for name, pos in sorted(cols.items(), key=lambda kv: kv[1]):
        if pos <= start + _TOL:
            best = name
    return best


def _is_table_end(line: str, dev_col: int) -> bool:
    s = line.strip()
    if not s:
        return False
    if _PAGE.search(s):
        return True
    if " = " in s or s.startswith("=") or re.match(r"^\S+\s+=", s):
        return True
    indent = len(line) - len(line.lstrip())
    return indent < dev_col - 1


# ── Row derivation (shared by the parser and the LLM fallback) ─────────


def _pct(text: str) -> int | None:
    m = _PCT.search(text or "")
    return int(m.group(1)) if m else None


def _reading(weekly: str, note: str) -> str:
    t = f"{weekly} {note}".lower().replace("‑", "-")
    if re.search(r"post[- ]?reset|after (?:free )?reset", t):
        return "post_reset"
    if re.search(r"in[- ]progress|mid[- ]cycle", t):
        return "mid_cycle"
    if "unverified" in t:
        return "unverified"
    return "completed"


def _strip_fable(text: str) -> str:
    t = re.sub(r"\(\s*fable[^)]*\)", " ", text, flags=re.IGNORECASE)
    return re.sub(r"fable\s+limit\s+(?:reached|hit)", " ", t, flags=re.IGNORECASE)


def _limit_status(weekly: str, note: str, weekly_pct: int | None) -> str:
    raw = f"{weekly} {_strip_fable(note)}"
    t = raw.lower()
    if "🔴" in raw or "limit hit" in t or re.search(r"\breached\b", t):
        return "hit"
    if weekly_pct is not None and weekly_pct >= 100:
        return "hit"
    if "projected" in t:
        return "projected"
    if "⚠" in raw or "near limit" in t or "near-limit" in t or "usage warning" in t:
        return "near"
    return "ok"


def derive_row(cells: dict[str, str]) -> dict[str, Any]:
    """Validated output row from raw cell text. Raises :class:`UsageParseError`."""
    dev_text = " ".join((cells.get("developer") or "").split())
    m = _DEV_ACCT.match(dev_text)
    developer, account = (
        (m.group("name").strip(), f"Acct {m.group('n')}") if m else (dev_text, None)
    )
    if cells.get("account") and account is None:
        account = str(cells["account"]).strip()
        if account.isdigit():
            account = f"Acct {account}"
    if not developer:
        raise UsageParseError("row without a developer")
    weekly = " ".join((cells.get("weekly") or "").split())
    session = " ".join((cells.get("session") or "").split())
    fable = " ".join((cells.get("fable") or "").split())
    note = " ".join((cells.get("note") or "").split())
    weekly_pct = _pct(weekly)
    session_pct = _pct(session)
    where = f"{developer} {account or ''}".strip()
    if weekly_pct is None:
        raise UsageParseError(f"{where}: no weekly pct in {weekly!r}")
    for label, v in (("weekly", weekly_pct), ("session", session_pct)):
        if v is not None and not 0 <= v <= 100:
            raise UsageParseError(f"{where}: {label} pct {v} outside 0-100")
    if fable:
        fable_pct = _pct(fable)
        fable_hit = bool(
            (fable_pct is not None and fable_pct >= 100)
            or re.search(r"limit", fable, re.IGNORECASE)
        )
    else:
        fm = re.search(r"fable\s*(\d{1,3})\s*%", note, re.IGNORECASE)
        fable_pct = int(fm.group(1)) if fm else None
        fable_hit = bool(re.search(r"fable\s+limit\s+(?:reached|hit)", note, re.IGNORECASE)) or (
            fable_pct is not None and fable_pct >= 100
        )
    if fable_pct is not None and not 0 <= fable_pct <= 100:
        raise UsageParseError(f"{where}: fable pct {fable_pct} outside 0-100")
    return {
        "developer": developer,
        "account": account,
        "plan": " ".join((cells.get("plan") or "").split()) or None,
        "weekly_pct": weekly_pct,
        "session_pct": session_pct,
        "fable_pct": fable_pct,
        "reset": " ".join((cells.get("reset") or "").split()) or None,
        "fable_limit_hit": fable_hit,
        "reading": _reading(weekly, note),
        "limit_status": _limit_status(weekly, note, weekly_pct),
        "note": note,
    }


# ── Deterministic parser ───────────────────────────────────────────────


def _report_meta(text: str) -> dict[str, Any]:
    head = "\n".join(text.splitlines()[:15])
    date = None
    for m in _DATE.finditer(head):
        try:
            date = datetime.strptime(  # noqa: DTZ007 - a calendar date, no time of day
                f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y"
            )
            break
        except ValueError:
            continue
    pm = _PERIOD.search(text)
    return {
        "report_date": date.strftime("%Y-%m-%d") if date else None,
        "period": pm.group(1) if pm else None,
    }


def parse_usage_text(text: str) -> dict[str, Any]:
    """Parse ``pdftotext -layout`` output. ``problems`` empty means consistent."""
    text = text.replace("\f", "\n")
    lines = text.splitlines()
    records: list[dict[str, list[str]]] = []
    problems: list[str] = []
    cols: dict[str, int] | None = None
    skip_next = False
    acct_tokens = 0

    for line in lines:
        hdr = _header_columns(line)
        if hdr:
            cols = hdr
            skip_next = True
            continue
        if cols is None:
            continue
        if skip_next:
            skip_next = False
            if "Account" in line or "Session" in line:
                continue
        if not line.strip():
            continue
        if _is_table_end(line, cols["developer"]):
            cols = None
            continue
        cells: dict[str, list[str]] = {}
        for start, chunk in _chunks(line):
            cells.setdefault(_assign(cols, start), []).append(chunk)
        dev = " ".join(cells.get("developer", []))
        acct_tokens += len(re.findall(r"\bAcct\b", dev))
        starts_row = _pct(" ".join(cells.get("weekly", []))) is not None or bool(
            re.search(r"[–—]", dev)
        )
        if starts_row or not records:
            records.append({c: [] for c in _COLS})
        for c, parts in cells.items():
            records[-1][c].extend(parts)

    rows: list[dict[str, Any]] = []
    for rec in records:
        try:
            rows.append(derive_row({c: " ".join(v) for c, v in rec.items()}))
        except UsageParseError as e:
            problems.append(str(e))
    if not records:
        problems.append("no usage table found (no 'Developer / Plan / Weekly' header row)")
    with_acct = sum(1 for r in rows if r["account"])
    if acct_tokens and with_acct != acct_tokens and not problems:
        problems.append(f"{acct_tokens} 'Acct' cells but {with_acct} account rows")
    seen: set[tuple[str, str | None]] = set()
    for r in rows:
        key = (r["developer"], r["account"])
        if key in seen:
            problems.append(f"duplicate row {key}")
        seen.add(key)
    return {**_report_meta(text), "rows": rows, "problems": problems}


# ── LLM fallback ───────────────────────────────────────────────────────

_LLM_PROMPT = (
    "Extract the weekly usage table from this report text. Return ONLY a JSON array, one "
    "object per table row, with string fields copied verbatim from the cells: developer "
    "(the name before 'Acct'), account (e.g. 'Acct 1'), plan, weekly (Weekly Usage cell), "
    "session (Current Session cell), fable (Fable cell, '' if no such column), reset "
    "(Weekly Reset cell), note (Status / Key Note cell). Join wrapped lines with a space. "
    "Do not compute or infer anything."
)


def _default_llm() -> Callable[[str], list[dict[str, Any]]] | None:
    from robothor.engine.reports.devops_team import devops_env

    model = devops_env("DEVOPS_USAGE_LLM_MODEL")
    if not model:
        return None

    def call(text: str) -> list[dict[str, Any]]:
        import asyncio

        from robothor.engine.pooled_completion import acompletion

        resp = asyncio.run(
            acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": _LLM_PROMPT},
                    {"role": "user", "content": text[:20000]},
                ],
                temperature=0,
                max_tokens=4000,
            )
        )
        content = resp.choices[0].message.content or ""
        m = re.search(r"\[.*\]", content, re.DOTALL)
        data = json.loads(m.group(0) if m else content)
        if not isinstance(data, list):
            raise UsageParseError("LLM did not return a JSON array")
        return data

    return call


def extract_usage(
    text: str, *, llm: Callable[[str], list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    """Rows from report text: deterministic parse, else one LLM extraction, else unavailable."""
    parsed = parse_usage_text(text)
    meta = {"report_date": parsed["report_date"], "period": parsed["period"]}
    if not parsed["problems"]:
        return {"available": True, "method": "parser", **meta, "rows": parsed["rows"]}
    reason = "parser: " + "; ".join(parsed["problems"][:3])
    llm = llm or _default_llm()
    if llm is None:
        return {
            "available": False,
            "reason": f"{reason} (no DEVOPS_USAGE_LLM_MODEL for fallback)",
            **meta,
        }
    try:
        cells = llm(text)
        rows = [derive_row({k: str(v or "") for k, v in c.items()}) for c in cells]
        if not rows:
            raise UsageParseError("LLM returned no rows")
    except Exception as e:  # noqa: BLE001 - any LLM failure means "unavailable"
        return {"available": False, "reason": f"{reason}; LLM fallback failed: {e}", **meta}
    return {
        "available": True,
        "method": "llm",
        "parser_problems": parsed["problems"],
        **meta,
        "rows": rows,
    }


# ── Fetch (gws CLI) ────────────────────────────────────────────────────


def _gws(
    run: Callable[..., dict[str, Any]], args: list[str], params: dict[str, Any]
) -> dict[str, Any]:
    result = run([*args, "--params", json.dumps(params)], timeout=60)
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(
            f"gws {' '.join(args[2:])}: {result.get('error') if isinstance(result, dict) else result}"
        )
    return result


def _find_pdf_part(payload: dict[str, Any]) -> dict[str, Any] | None:
    stack = [payload]
    while stack:
        part = stack.pop()
        name = (part.get("filename") or "").lower()
        is_pdf = name.endswith(".pdf") or part.get("mimeType") == "application/pdf"
        if is_pdf and (part.get("body") or {}).get("attachmentId"):
            return part
        stack.extend(part.get("parts") or [])
    return None


def fetch_latest_report(
    senders: list[str],
    *,
    run: Callable[..., dict[str, Any]] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Newest matching email's PDF: {message_id, pdf, subject, sender, internal_date}.

    Raises LookupError when no matching email with a PDF exists.
    """
    if run is None:
        from robothor.engine.tools.handlers.gws import run_gws

        run = run_gws
    if message_id:
        ids = [message_id]
    else:
        if not senders:
            raise LookupError("no senders configured (DEVOPS_USAGE_REPORT_SENDERS)")
        from_q = " OR ".join(f"from:{s}" for s in senders)
        q = f'({from_q}) subject:"{SUBJECT_HINT}" has:attachment newer_than:{LOOKBACK_DAYS}d'
        listed = _gws(
            run,
            ["gmail", "users", "messages", "list"],
            {"userId": "me", "q": q, "maxResults": 5},
        )
        ids = [m["id"] for m in listed.get("messages") or [] if m.get("id")]
    candidates = []
    for mid in ids:
        msg = _gws(run, ["gmail", "users", "messages", "get"], {"userId": "me", "id": mid})
        part = _find_pdf_part(msg.get("payload") or {})
        if part is None:
            continue
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (msg.get("payload") or {}).get("headers") or []
        }
        candidates.append((int(msg.get("internalDate") or 0), mid, part, headers))
    if not candidates:
        raise LookupError(
            f"no '{SUBJECT_HINT}' email with a PDF in the last {LOOKBACK_DAYS} days"
            if not message_id
            else f"message {message_id} has no PDF attachment"
        )
    internal, mid, part, headers = max(candidates, key=lambda c: c[0])
    att = _gws(
        run,
        ["gmail", "users", "messages", "attachments", "get"],
        {"userId": "me", "messageId": mid, "id": part["body"]["attachmentId"]},
    )
    data = att.get("data") or ""
    pdf = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return {
        "message_id": mid,
        "pdf": pdf,
        "subject": headers.get("subject", ""),
        "sender": headers.get("from", ""),
        "internal_date": internal,
    }


def pdf_to_text(pdf_path: Path) -> str:
    proc = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {proc.stderr.strip()[:200]}")
    return proc.stdout
