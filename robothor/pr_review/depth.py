"""How much review a pull request earns: full, light, or none at all.

* ``SKIP`` — bot authors (dependency bumps), an operator-configured skip
  label (none by default: a label anyone can add must not switch the review
  off unless the operator says so), or a change made only of lockfiles and
  generated files. No task is created.
* ``FULL`` — risky labels (security, auth, billing, migration) or anything
  past the small-change threshold.
* ``LIGHT`` — a small change: one reviewer walks the lenses without splitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["Depth", "DepthPolicy"]


class Depth(StrEnum):
    FULL = "full"
    LIGHT = "light"
    SKIP = "skip"


@dataclass(frozen=True)
class DepthPolicy:
    """Rules that map a pull request's shape to a review depth."""

    small_max_lines: int = 50
    risky_labels: tuple[str, ...] = ("billing", "security", "auth", "migration")
    skip_labels: tuple[str, ...] = ()
    skip_authors: tuple[str, ...] = ("dependabot[bot]", "renovate[bot]")
    #: Exact file names (``package-lock.json``), suffixes (``.snap``, ``.min.js``)
    #: and infixes wrapped in dots (``.generated.``). See :meth:`is_generated`.
    skip_paths: tuple[str, ...] = (
        "package-lock.json",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "go.sum",
        ".min.js",
        ".min.css",
        ".generated.",
        ".snap",
    )

    def depth_for(
        self,
        *,
        labels: Iterable[str] = (),
        author: str = "",
        changed_lines: int = 0,
        files: Iterable[str] = (),
    ) -> Depth:
        """Classify one pull request. A skip wins over a risky label."""
        low_labels = {label.lower() for label in labels}
        file_list = list(files)
        if author.lower() in {a.lower() for a in self.skip_authors}:
            return Depth.SKIP
        if low_labels & {label.lower() for label in self.skip_labels}:
            return Depth.SKIP
        if file_list and all(self.is_generated(f) for f in file_list):
            return Depth.SKIP
        if low_labels & {label.lower() for label in self.risky_labels}:
            return Depth.FULL
        if changed_lines and changed_lines <= self.small_max_lines:
            return Depth.LIGHT
        return Depth.FULL

    def depth_for_pr(self, *, pr: Mapping[str, Any], files: Iterable[Mapping[str, Any]]) -> Depth:
        """:meth:`depth_for` from GitHub's pull-request and changed-file objects."""
        file_list = list(files)
        return self.depth_for(
            labels=[str((lab or {}).get("name") or "") for lab in pr.get("labels") or []],
            author=str((pr.get("user") or {}).get("login") or ""),
            changed_lines=sum(
                int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in file_list
            ),
            files=[str(f.get("filename") or "") for f in file_list],
        )

    def is_generated(self, path: str) -> bool:
        """Whether ``path`` is a lockfile or generated file, by its base name.

        A marker like ``.snap`` is a suffix: ``a.test.ts.snap`` is generated,
        ``snapshot.snapshot.ts`` is code. ``.generated.`` matches inside the
        name; a plain name like ``uv.lock`` must be the whole base name.
        """
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        for marker in (m.lower() for m in self.skip_paths):
            if marker.startswith(".") and marker.endswith(".") and len(marker) > 1:
                if marker in name:
                    return True
            elif marker.startswith("."):
                if name.endswith(marker):
                    return True
            elif name == marker:
                return True
        return False
