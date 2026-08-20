"""Assertions for the documentation site (mkdocs.yml + .github/workflows/docs.yml).

The docs workflow deploys to GitHub Pages, so it must stay immutably pinned
and permission-scoped, and the site must stay an explicit allowlist -- docs/
also holds internal material (runbooks, benchmarks, plans, agent schemas)
that must never publish by default.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docs.yml"
MKDOCS_PATH = REPO_ROOT / "mkdocs.yml"

IMMUTABLE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _load_mkdocs() -> dict:
    # mkdocs.yml may use custom tags (!ENV, python names); ignore them.
    class _Loose(yaml.SafeLoader):
        pass

    _Loose.add_multi_constructor("", lambda loader, suffix, node: None)
    return yaml.load(MKDOCS_PATH.read_text(encoding="utf-8"), Loader=_Loose)


def _iter_uses(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_iter_uses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_uses(item))
    return found


def test_docs_workflow_actions_are_sha_pinned() -> None:
    uses = _iter_uses(_load_workflow())
    assert uses, "docs workflow should invoke at least one action"
    for target in uses:
        if target.startswith("./"):
            continue
        _, separator, revision = target.rpartition("@")
        assert separator, f"unpinned action ref: {target}"
        assert IMMUTABLE_COMMIT.fullmatch(revision), (
            f"action not pinned to a full 40-char commit SHA: {target}"
        )


def test_docs_workflow_permissions_are_scoped() -> None:
    workflow = _load_workflow()
    assert workflow.get("permissions") == {"contents": "read"}, (
        "top-level permissions must be exactly contents: read"
    )

    jobs = workflow["jobs"]
    assert "permissions" not in jobs["build"], "build job must inherit the read-only default"
    assert jobs["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }, "pages/id-token write must be scoped to the deploy job only"


def test_docs_workflow_deploys_only_from_main_push() -> None:
    deploy = _load_workflow()["jobs"]["deploy"]
    condition = deploy.get("if", "")
    assert "pull_request" in condition and "refs/heads/main" in condition, (
        "deploy job must be gated to push events on main"
    )
    assert deploy.get("environment", {}).get("name") == "github-pages"


def test_docs_workflow_builds_strict() -> None:
    build = _load_workflow()["jobs"]["build"]
    commands = [step.get("run", "") for step in build["steps"]]
    assert any("mkdocs build --strict" in command for command in commands), (
        "docs build must run mkdocs in strict mode so broken links fail"
    )


def test_mkdocs_site_is_an_allowlist() -> None:
    config = _load_mkdocs()
    exclude = config.get("exclude_docs") or ""
    lines = [line.strip() for line in exclude.splitlines() if line.strip()]
    assert lines and lines[0] == "/*", (
        "exclude_docs must exclude everything by default (allowlist model)"
    )
    assert all(line.startswith("!") for line in lines[1:]), (
        "after the /* catch-all, every entry must be a re-include"
    )


def test_mkdocs_nav_pages_exist_and_are_allowlisted() -> None:
    config = _load_mkdocs()
    allowlisted = {
        line.strip().lstrip("!/")
        for line in (config.get("exclude_docs") or "").splitlines()
        if line.strip().startswith("!")
    }

    def _pages(node) -> list[str]:
        pages: list[str] = []
        if isinstance(node, str):
            pages.append(node)
        elif isinstance(node, list):
            for item in node:
                pages.extend(_pages(item))
        elif isinstance(node, dict):
            for value in node.values():
                pages.extend(_pages(value))
        return pages

    nav_pages = _pages(config.get("nav"))
    assert nav_pages, "mkdocs nav must not be empty"
    for page in nav_pages:
        assert (REPO_ROOT / "docs" / page).is_file(), f"nav page missing: docs/{page}"
        assert page in allowlisted, f"nav page not in exclude_docs allowlist: {page}"


def test_mkdocs_uses_default_pages_url() -> None:
    config = _load_mkdocs()
    assert config.get("site_url", "").startswith("https://ironsail-llc.github.io/"), (
        "site_url must stay on the default github.io host (no custom domain DNS)"
    )
