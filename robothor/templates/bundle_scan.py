"""What a stranger's agent bundle is, before an instance agrees to run it.

A plugin wheel that arrives through the signed index is scanned
(:mod:`robothor.plugins.scan`), gets a verdict recorded at publish time, has
that scan RE-RUN on the bytes actually downloaded, and is refused outright on
``blocked`` with ``review`` behind an explicit flag. An agent bundle arriving
through the same signed document had none of it — and a bundle is a prompt an
autonomous agent executes with whatever tools its manifest grants, which is
precisely what a prompt scan is for.

So: the same three verdicts, the same enforcement, the same "the publisher's
verdict is advisory and the installer re-runs its own".

**What blocks.** Anything the export gate would have refused: a credential
literal, or a path that only exists on the exporting instance. A bundle
carrying one of those either was not produced by ``genus agent export`` or was
edited afterwards — both are reasons to stop rather than to warn.

**What needs review.** A capability grant a human should look at before it runs
on their appliance: a tool that can execute, write, send or reach the network;
an absent ``tools_allowed`` list, which the engine reads as *every* tool rather
than as none; the ability to spawn sub-agents.

**What this is not.** It is not a judgement about whether the instructions are
*good*, and it does not read intent. It answers "did a human agree to this
capability grant", which is the question an install can actually ask.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from robothor.templates.bundle import (
    BUNDLE_FILENAME,
    scan_instance_leaks,
    scan_secret_literals,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "BLOCKED",
    "REVIEW",
    "SAFE",
    "BundleScanError",
    "BundleVerdict",
    "enforce_verdict",
    "scan_bundle",
]

SAFE = "safe"
REVIEW = "review"
BLOCKED = "blocked"

#: Ordered worst-first, so a scan can keep the strongest verdict it saw.
_SEVERITY = {SAFE: 0, REVIEW: 1, BLOCKED: 2}

_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


class BundleScanError(Exception):
    """A refusal, in the sentence the operator gets told."""


@dataclass(frozen=True)
class BundleVerdict:
    """One scan result: a verdict and the reasons behind it.

    ``reasons`` name files and capabilities, never content — the same rule the
    export gate follows, for the same reason: this string reaches a terminal,
    an audit row and a signed index.
    """

    verdict: str = SAFE
    reasons: tuple[str, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reasons": list(self.reasons)}


def _raise_to(current: str, candidate: str) -> str:
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


def _manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.template.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(_PLACEHOLDER.sub("TEMPLATE_VALUE", path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_bundle(directory: str | Path, *, members: Sequence[str] | None = None) -> BundleVerdict:
    """Scan every member of the bundle at *directory* and return one verdict.

    Never raises on content: an unreadable member is decoded with replacement
    and scanned anyway. A scanner that refused to look at a file because it was
    not valid UTF-8 would be a scanner an attacker could turn off with one
    stray byte.
    """
    from robothor.templates.bundle_installer import is_high_risk

    root = Path(directory)
    verdict = SAFE
    reasons: list[str] = []

    paths = (
        [root / member for member in members]
        if members is not None
        else sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    )
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == BUNDLE_FILENAME:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for finding in (
            *scan_secret_literals(text, relative),
            *scan_instance_leaks(text, relative),
        ):
            verdict = _raise_to(verdict, BLOCKED)
            reasons.append(finding.describe())

    declared = _manifest(root)
    raw_tools = declared.get("tools_allowed")
    tools = [str(tool) for tool in raw_tools] if isinstance(raw_tools, list) else []
    if not tools:
        verdict = _raise_to(verdict, REVIEW)
        reasons.append(
            "the manifest grants no tools_allowed list, which the engine reads as "
            "every tool this fleet has"
        )
    else:
        flagged = [tool for tool in tools if is_high_risk(tool)]
        if flagged:
            verdict = _raise_to(verdict, REVIEW)
            reasons.append(f"tools that act on the world: {', '.join(sorted(flagged))}")

    v2 = declared.get("v2")
    if isinstance(v2, dict) and v2.get("can_spawn_agents"):
        verdict = _raise_to(verdict, REVIEW)
        reasons.append("the agent may spawn sub-agents")

    return BundleVerdict(verdict=verdict, reasons=tuple(reasons))


def enforce_verdict(verdict: BundleVerdict, *, accept_review: bool) -> None:
    """Act on a verdict. ``blocked`` always refuses; ``review`` needs a flag.

    ``accept_review`` deliberately does not cover ``blocked``: a verdict an
    operator can wave through with the flag they already type out of habit is a
    verdict that stopped meaning anything.
    """
    detail = "\n  - ".join(verdict.reasons)
    if verdict.verdict == BLOCKED:
        raise BundleScanError(
            "This bundle is blocked and will not be installed:\n  - "
            f"{detail}\n"
            "It carries something no export should have produced. Ask whoever sent it "
            "to re-export with 'genus agent export', which refuses these outright."
        )
    if verdict.verdict == REVIEW and not accept_review:
        raise BundleScanError(
            "This bundle needs review before it runs on this instance:\n  - "
            f"{detail}\n"
            "Read the plan above, and pass --accept-review if you mean to grant it."
        )
