"""The review verdict, recomputed in code from the findings. Pure functions only.

The model proposes a verdict; this module decides it. The rules:

* Any ``blocker`` or ``major`` finding — new, or a previous one the model did
  not explicitly report ``resolved`` — makes the verdict the configured
  blocking event (``REQUEST_CHANGES``, or ``COMMENT`` for an instance that
  never wants to block a merge). Never ``APPROVE``. Silence is not a fix: a
  previous blocking finding the model leaves out of ``prior_issues``, or
  reports with any status other than ``resolved``, still blocks.
* ``APPROVE`` is posted only when the model itself approved AND nothing
  blocking remains. ``minor`` findings and nits never stand in the way.
* ``REQUEST_CHANGES`` with nothing blocking has no basis and becomes
  ``COMMENT``.
* Optional ticket rule: with ``require_ticket`` on, a pull request whose
  title, branch, body and commit messages carry no ticket key gets a blocking
  ``[no-ticket]`` finding, so it is never approved.

:func:`guard_posted_verdict` is the narrower rule the generic
``github_create_review`` tool applies to every caller: it refuses only the one
combination that is never right — APPROVE alongside a blocking finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "BLOCKING_SEVERITIES",
    "NO_TICKET_TITLE",
    "VERDICTS",
    "PolicyResult",
    "decide_verdict",
    "extract_ticket_key",
    "guard_posted_verdict",
    "is_blocking",
]

VERDICTS: frozenset[str] = frozenset({"APPROVE", "COMMENT", "REQUEST_CHANGES"})
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})
#: The only previous-finding status that clears a blocking finding.
_RESOLVED = "resolved"
_BLOCKING_EVENTS: frozenset[str] = frozenset({"REQUEST_CHANGES", "COMMENT"})

NO_TICKET_TITLE = "[no-ticket] No linked ticket"
_NO_TICKET_BODY = (
    "No ticket key was found in the title, branch, description or commit messages. "
    "Every pull request needs a linked ticket to review against — link one and ask "
    "for a re-review."
)


def _severity(issue: Mapping[str, Any]) -> str:
    return str(issue.get("severity") or "").strip().lower()


def is_blocking(issue: Mapping[str, Any]) -> bool:
    return _severity(issue) in BLOCKING_SEVERITIES


@dataclass(frozen=True)
class PolicyResult:
    """What will be posted, and why it differs from what the model said."""

    verdict: str
    model_verdict: str
    issues: list[dict[str, Any]]
    blocking: list[dict[str, Any]] = field(default_factory=list)
    prior_blocking: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: Previous comment ids the model explicitly reported ``resolved``.
    resolved_ids: list[int] = field(default_factory=list)

    @property
    def overridden(self) -> bool:
        return self.verdict != self.model_verdict


def decide_verdict(
    model_verdict: str,
    issues: Iterable[Mapping[str, Any]],
    *,
    prior_issues: Iterable[Mapping[str, Any]] | None = None,
    prior_severities: Mapping[int, str] | None = None,
    blocking_event: str = "REQUEST_CHANGES",
    require_ticket: bool = False,
    ticket_key: str | None = None,
) -> PolicyResult:
    """The verdict to post. See the module docstring for the rules."""
    proposed = str(model_verdict or "").strip().upper()
    event = str(blocking_event or "").strip().upper()
    if event not in _BLOCKING_EVENTS:
        event = "REQUEST_CHANGES"
    out_issues = [dict(i) for i in issues]
    reasons: list[str] = []

    if require_ticket and not ticket_key:
        out_issues.append(
            {
                "path": "",
                "line": None,
                "start_line": None,
                "side": "RIGHT",
                "severity": "blocker",
                "title": NO_TICKET_TITLE,
                "body": _NO_TICKET_BODY,
            }
        )
        reasons.append("no linked ticket")

    blocking = [i for i in out_issues if is_blocking(i)]
    severities = {int(k): str(v).lower() for k, v in (prior_severities or {}).items()}
    reported: dict[int, Mapping[str, Any]] = {}
    for prior in prior_issues or []:
        cid = prior.get("comment_id")
        if isinstance(cid, int) and not isinstance(cid, bool) and cid not in reported:
            reported[cid] = prior
    resolved_ids = [
        cid
        for cid, prior in reported.items()
        if cid in severities and str(prior.get("status") or "").strip().lower() == _RESOLVED
    ]
    prior_blocking: list[dict[str, Any]] = []
    for cid, severity in severities.items():
        if severity not in BLOCKING_SEVERITIES or cid in resolved_ids:
            continue
        found = reported.get(cid)
        prior_blocking.append(
            dict(found) if found else {"comment_id": cid, "status": "unreported", "note": ""}
        )
    if any(p.get("status") == "unreported" for p in prior_blocking):
        reasons.append("a previous blocking finding was not reported resolved")

    if blocking or prior_blocking:
        verdict = event
        if proposed == "APPROVE":
            reasons.append("model approved with blocking findings")
    elif proposed == "APPROVE":
        verdict = "APPROVE"
    else:
        verdict = "COMMENT"
        if proposed == "REQUEST_CHANGES":
            reasons.append("changes requested without a blocking finding")
    return PolicyResult(
        verdict=verdict,
        model_verdict=proposed,
        issues=out_issues,
        blocking=blocking,
        prior_blocking=prior_blocking,
        reasons=reasons,
        resolved_ids=resolved_ids,
    )


def guard_posted_verdict(verdict: str, issues: Iterable[Mapping[str, Any]]) -> tuple[str, bool]:
    """``(verdict, overridden)``: APPROVE beside a blocking finding becomes REQUEST_CHANGES."""
    if str(verdict).upper() == "APPROVE" and any(is_blocking(i) for i in issues):
        return "REQUEST_CHANGES", True
    return verdict, False


#: Any Jira-style key when no prefix is configured. Uppercase only, so that
#: ``utf-8`` or ``sha-256`` in a title is not mistaken for a ticket.
_ANY_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})-(\d+)(?![0-9])")


def _prefixed_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(prefix)}[-_ ]?(\d+)(?![0-9])", re.IGNORECASE)


def extract_ticket_key(
    prefixes: Sequence[str],
    *,
    title: str | None = None,
    branch: str | None = None,
    body: str | None = None,
    commit_messages: Iterable[str] = (),
) -> str | None:
    """The first ticket key in title, then branch, then body, then commit messages.

    With ``prefixes`` (e.g. ``["ABC"]``) a key is ``ABC-<n>``, matched without
    regard to case so a conventional-commit branch like ``feat/abc-7-cart``
    still counts; ``ABC_7`` and ``ABC 7`` are accepted too. Without prefixes any
    uppercase ``KEY-<n>`` counts.
    """
    wanted = [p.strip() for p in prefixes if p and p.strip()]
    sources = [title, branch, body, *commit_messages]
    for text in sources:
        if not text:
            continue
        if wanted:
            for prefix in wanted:
                m = _prefixed_re(prefix).search(text)
                if m:
                    return f"{prefix.upper()}-{int(m.group(1))}"
        else:
            m = _ANY_KEY_RE.search(text)
            if m:
                return f"{m.group(1)}-{int(m.group(2))}"
    return None
