"""The gate that turns a commit list into release notes a reader can use.

`CHANGELOG.md` is the developer record: one line per conventional commit. An
operator reading "feat(bridge): memory forget, flag audit, CSV export and logs
API" cannot tell whether anything changed for them, and nothing in the release
pipeline ever asked the author to say.

`changelog.d/<PR>.<audience>.md` is where the author says it, in the reader's
own words, and `scripts/changelog_fragments.py` is the three verbs around it:
`check` (CI refuses a feat/fix/perf PR with no fragment and no opt-out label),
`lint` (the fragment is addressed to a real audience, is two to four sentences,
carries no absolute path or personal data, and every page it names resolves),
and `assemble` (the release groups them by audience into `docs/release-notes.md`
and deletes what it consumed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "changelog_fragments.py"

#: A fragment that satisfies every lint rule, so a test that wants to break
#: exactly one rule can start from something known-good.
GOOD = (
    "You can now install a plugin from a signed registry without a shell. "
    "The Helm's Settings, Plugins page previews the plan first and refuses a "
    "blocked verdict outright. See [Plugins](PLUGINS.md) for what a verdict means."
)


def _module():
    spec = importlib.util.spec_from_file_location("changelog_fragments", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module's dataclasses use PEP 563 annotations,
    # which `dataclasses` resolves through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _module()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway tree with the two paths the tool reads and writes."""
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "release-notes.md").write_text(
        "# Release notes\n\nWhat each release changed, by who it changed it for.\n",
        encoding="utf-8",
    )
    return tmp_path


def _write(repo: Path, name: str, text: str = GOOD) -> Path:
    path = repo / "changelog.d" / name
    path.write_text(text + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# check: a feat/fix/perf PR must carry a fragment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["feat(helm): x", "fix: y", "perf(engine): z"])
def test_check_fails_when_a_releasing_pr_has_no_fragment(tool, repo, title) -> None:
    problems = tool.check(repo, pr=42, title=title, labels=[])
    assert problems, f"{title} must require a fragment"
    assert "changelog.d/42." in problems[0]
    assert "no-changelog" in problems[0]


def test_check_passes_when_the_fragment_is_there(tool, repo) -> None:
    _write(repo, "42.operators.md")
    assert tool.check(repo, pr=42, title="feat(helm): x", labels=[]) == []


def test_check_passes_with_the_opt_out_label(tool, repo) -> None:
    assert tool.check(repo, pr=42, title="feat(helm): x", labels=["no-changelog"]) == []


@pytest.mark.parametrize("title", ["docs: x", "chore(deps): y", "ci: z", "refactor: w"])
def test_check_ignores_types_that_do_not_ship_a_change(tool, repo, title) -> None:
    assert tool.check(repo, pr=42, title=title, labels=[]) == []


def test_check_sees_a_breaking_change_title(tool, repo) -> None:
    assert tool.check(repo, pr=42, title="feat(api)!: x", labels=[]) != []


def test_check_does_not_accept_another_prs_fragment(tool, repo) -> None:
    _write(repo, "41.operators.md")
    assert tool.check(repo, pr=42, title="fix: y", labels=[]) != []


def test_check_reports_a_bad_fragment_even_when_one_exists(tool, repo) -> None:
    _write(repo, "42.everyone.md")
    problems = tool.check(repo, pr=42, title="feat: x", labels=[])
    assert problems and "everyone" in " ".join(problems)


def test_labels_may_arrive_as_a_json_array(tool) -> None:
    assert tool.parse_labels('["no-changelog", "deploy-staging"]') == [
        "no-changelog",
        "deploy-staging",
    ]


def test_labels_may_arrive_comma_separated(tool) -> None:
    assert tool.parse_labels("no-changelog, deploy-staging") == [
        "no-changelog",
        "deploy-staging",
    ]


def test_no_labels_is_an_empty_list(tool) -> None:
    assert tool.parse_labels("") == []
    assert tool.parse_labels(None) == []


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------


def _lint(tool, repo: Path, known=frozenset({"docs/PLUGINS.md", "docs/quickstart.md"})):
    return tool.lint(repo, known=known)


def test_lint_accepts_a_good_fragment(tool, repo) -> None:
    _write(repo, "42.operators.md")
    assert _lint(tool, repo) == []


def test_lint_accepts_every_documented_audience(tool, repo) -> None:
    for index, audience in enumerate(tool.AUDIENCES):
        text = GOOD if audience != "internal" else "Nothing changed for a reader. Internal only."
        _write(repo, f"{40 + index}.{audience}.md", text)
    assert _lint(tool, repo) == []


def test_lint_rejects_an_unknown_audience(tool, repo) -> None:
    _write(repo, "42.everyone.md")
    findings = _lint(tool, repo)
    assert findings and "everyone" in str(findings[0])


def test_lint_rejects_a_filename_that_is_not_pr_audience(tool, repo) -> None:
    _write(repo, "notes.md")
    assert _lint(tool, repo) != []


def test_lint_ignores_the_directory_readme(tool, repo) -> None:
    (repo / "changelog.d" / "README.md").write_text("How to write one.\n", encoding="utf-8")
    assert _lint(tool, repo) == []


def test_lint_rejects_one_sentence(tool, repo) -> None:
    _write(repo, "42.operators.md", "You can now install a plugin from [here](PLUGINS.md).")
    findings = _lint(tool, repo)
    assert findings and "sentence" in str(findings[0])


def test_lint_rejects_five_sentences(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "One thing happened. Two things happened. Three things happened. "
        "Four things happened. Five, see [Plugins](PLUGINS.md).",
    )
    findings = _lint(tool, repo)
    assert findings and "sentence" in str(findings[0])


def test_lint_does_not_count_a_version_number_as_a_sentence_end(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "The chart now publishes to an OCI registry at version 1.90.0. "
        "See [Quick Start](quickstart.md).",
    )
    assert _lint(tool, repo) == []


def test_lint_rejects_a_home_path(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "The workspace now defaults to /home/operator/robothor on a fresh install. "
        "See [Quick Start](quickstart.md).",
    )
    findings = _lint(tool, repo)
    assert findings and "absolute path" in str(findings[0])


def test_lint_rejects_a_mac_home_path(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "It reads /Users/operator/genus now, which is new. See [Quick Start](quickstart.md).",
    )
    assert _lint(tool, repo) != []


def test_lint_allows_a_standard_system_path(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "The units now read /etc/robothor/robothor.env before they start. "
        "See [Quick Start](quickstart.md).",
    )
    assert _lint(tool, repo) == []


def test_lint_rejects_a_personal_email(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "Mail now goes out as operator@acme-holdings.test by default. "
        "See [Quick Start](quickstart.md).",
    )
    findings = _lint(tool, repo)
    assert findings and "personal data" in str(findings[0])


def test_lint_allows_a_fixture_email(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "The invite now shows the address, such as agent@example.com, before it sends. "
        "See [Quick Start](quickstart.md).",
    )
    assert _lint(tool, repo) == []


def test_lint_rejects_a_phone_number(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "The pager now dials 555-867-5309 on a failure. See [Quick Start](quickstart.md).",
    )
    assert _lint(tool, repo) != []


def test_lint_rejects_a_link_that_does_not_resolve(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "You can now do the thing without a shell. See [Nowhere](nowhere.md) for how.",
    )
    findings = _lint(tool, repo)
    assert findings and "nowhere" in str(findings[0]).lower()


def test_lint_resolves_links_from_the_release_notes_page_not_the_fragment_dir(tool, repo) -> None:
    """A fragment's links are written for where they will be published."""
    _write(
        repo,
        "42.operators.md",
        "You can now install from a registry. See [Plugins](PLUGINS.md) for the verdicts.",
    )
    assert _lint(tool, repo) == []


def test_lint_requires_a_reader_facing_fragment_to_name_a_page(tool, repo) -> None:
    _write(
        repo,
        "42.operators.md",
        "You can now install a plugin from a signed registry. It refuses a blocked verdict.",
    )
    findings = _lint(tool, repo)
    assert findings and "page" in str(findings[0])


def test_lint_does_not_require_a_page_of_an_internal_fragment(tool, repo) -> None:
    _write(repo, "42.internal.md", "The loader was refactored. No reader-facing change.")
    assert _lint(tool, repo) == []


def test_lint_rejects_an_empty_fragment(tool, repo) -> None:
    _write(repo, "42.operators.md", "")
    assert _lint(tool, repo) != []


# --------------------------------------------------------------------------
# assemble
# --------------------------------------------------------------------------


def _notes(repo: Path) -> str:
    return (repo / "docs" / "release-notes.md").read_text(encoding="utf-8")


def test_assemble_writes_a_version_block(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert "## 1.91.0 — 2026-09-15" in _notes(repo)


def test_assemble_groups_by_audience_in_a_fixed_order(tool, repo) -> None:
    _write(repo, "40.agent-authors.md")
    _write(repo, "41.admins.md")
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    text = _notes(repo)
    assert text.index("### For operators") < text.index("### For admins")
    assert text.index("### For admins") < text.index("### For agent authors")


def test_assemble_omits_a_section_nobody_wrote_for(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert "### For admins" not in _notes(repo)


def test_assemble_orders_fragments_by_pr_within_a_section(tool, repo) -> None:
    _write(repo, "50.operators.md", "Second thing shipped. See [Plugins](PLUGINS.md).")
    _write(repo, "40.operators.md", "First thing shipped. See [Plugins](PLUGINS.md).")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    text = _notes(repo)
    assert text.index("First thing shipped") < text.index("Second thing shipped")


def test_assemble_links_each_fragment_to_its_pull_request(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert "[#42](https://github.com/Ironsail-llc/genus-os/pull/42)" in _notes(repo)


def test_assemble_deletes_the_fragments_it_consumed(tool, repo) -> None:
    path = _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert not path.exists()


def test_assemble_consumes_an_internal_fragment_without_publishing_it(tool, repo) -> None:
    internal = _write(repo, "43.internal.md", "The loader was refactored. No reader change.")
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert not internal.exists()
    assert "refactored" not in _notes(repo)


def test_assemble_keeps_the_readme(tool, repo) -> None:
    readme = repo / "changelog.d" / "README.md"
    readme.write_text("How to write one.\n", encoding="utf-8")
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert readme.exists()


def test_assemble_prepends_newer_releases_above_older_ones(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    _write(repo, "43.operators.md")
    tool.assemble(repo, version="1.92.0", date="2026-09-20")
    text = _notes(repo)
    assert text.index("## 1.92.0") < text.index("## 1.91.0")


def test_assemble_keeps_the_page_preamble_on_top(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert _notes(repo).startswith("# Release notes\n")


def test_assemble_with_no_fragments_changes_nothing(tool, repo) -> None:
    before = _notes(repo)
    assert tool.assemble(repo, version="1.91.0", date="2026-09-15") is None
    assert _notes(repo) == before


def test_assemble_is_idempotent(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    once = _notes(repo)
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    assert _notes(repo) == once


def test_assemble_refuses_to_write_a_version_block_twice(tool, repo) -> None:
    _write(repo, "42.operators.md")
    tool.assemble(repo, version="1.91.0", date="2026-09-15")
    _write(repo, "43.operators.md")
    with pytest.raises(tool.AssembleError):
        tool.assemble(repo, version="1.91.0", date="2026-09-16")


def test_assemble_refuses_a_fragment_that_does_not_lint(tool, repo) -> None:
    _write(repo, "42.everyone.md")
    with pytest.raises(tool.AssembleError):
        tool.assemble(repo, version="1.91.0", date="2026-09-15", known=frozenset())


# --------------------------------------------------------------------------
# preview: what the sticky PR comment says
# --------------------------------------------------------------------------


def test_preview_quotes_the_fragment(tool, repo) -> None:
    _write(repo, "42.operators.md")
    body = tool.preview(repo, pr=42, title="feat(helm): x", labels=[])
    assert "For operators" in body
    assert "signed registry" in body


def test_preview_says_what_to_do_when_there_is_no_fragment(tool, repo) -> None:
    body = tool.preview(repo, pr=42, title="feat(helm): x", labels=[])
    assert "no fragment" in body
    assert "no-changelog" in body


def test_preview_is_quiet_for_a_type_that_needs_no_fragment(tool, repo) -> None:
    body = tool.preview(repo, pr=42, title="chore(deps): x", labels=[])
    assert "no fragment" not in body


# --------------------------------------------------------------------------
# The repository's own fragments must always be shippable
# --------------------------------------------------------------------------


def test_the_repositorys_own_fragments_lint() -> None:
    tool = _module()
    assert tool.lint(REPO_ROOT) == []


# --------------------------------------------------------------------------
# The wiring. A gate nothing runs is not a gate.
# --------------------------------------------------------------------------

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PREVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-preview.yml"
RELEASERC = REPO_ROOT / ".releaserc.js"


def test_ci_runs_check_and_lint_on_every_pull_request() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "changelog-fragment:" in workflow
    assert "changelog_fragments.py lint" in workflow
    assert "changelog_fragments.py check" in workflow


def test_the_sticky_comment_shows_the_fragment() -> None:
    workflow = PREVIEW_WORKFLOW.read_text(encoding="utf-8")
    assert "changelog_fragments.py preview" in workflow
    assert "actions/checkout@" in workflow, "preview needs the tree to read the fragment"


def test_the_release_assembles_and_commits_both_halves() -> None:
    releaserc = RELEASERC.read_text(encoding="utf-8")
    assert "changelog_fragments.py assemble --version ${nextRelease.version}" in releaserc
    # The deletions of consumed fragments must ride in the release commit, or
    # the next release assembles them a second time.
    assert "'changelog.d'" in releaserc
    assert "'docs/release-notes.md'" in releaserc


def test_the_release_notes_page_is_published() -> None:
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "!/release-notes.md" in mkdocs, "the page the release writes must be on the site"
    assert "release-notes.md" in mkdocs.split("nav:", 1)[1]
