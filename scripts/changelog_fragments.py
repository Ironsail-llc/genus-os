#!/usr/bin/env python3
"""Release notes written for a reader, not for a commit log.

`CHANGELOG.md` is generated from conventional commits and stays that way: it
is the developer record. It is not release notes. An operator who reads
"feat(bridge): memory forget, flag audit, CSV export and logs API" cannot tell
whether anything changed for them, where to look, or what to do -- and nothing
in the pipeline had ever asked the author to say.

So the author says it, once, in the pull request that ships the change:

    changelog.d/<PR>.<audience>.md

`<audience>` is one of `operators` (people who run an instance), `admins`
(people who configure agents, users and channels in the Helm), `agent-authors`
(people who write manifests, skills and plugins) or `internal` (no user-facing
change -- recorded, never published). Two to four sentences, written from the
reader's side, naming the page they should open.

Three verbs:

  check     One pull request. A `feat`/`fix`/`perf` (or breaking) title must
            carry a fragment unless the PR is labelled `no-changelog`. This is
            the CI gate.
  lint      Every fragment in the tree: real audience, two to four sentences,
            no absolute path, no personal data, every page it names resolves.
  assemble  The release. Groups the fragments by audience into a dated version
            block at the top of `docs/release-notes.md` -- the page mkdocs
            publishes -- and deletes the fragments it consumed.

Exit code 0 = clean, 1 = something to fix, 2 = the command was wrong.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Who a change can be for. Order is the order sections appear in the notes.
AUDIENCES = ("operators", "admins", "agent-authors", "internal")

#: Section headings, in `AUDIENCES` order. `internal` has none on purpose: an
#: internal fragment is consumed by the release and never published, so the
#: decision is recorded without pretending it changed something for a reader.
SECTION_TITLES = {
    "operators": "For operators",
    "admins": "For admins",
    "agent-authors": "For agent authors",
}

#: Conventional-commit types that ship something a reader can notice. Mirrors
#: the release rules in `.releaserc.js`; a breaking `!` title counts whatever
#: its type.
RELEASING_TYPES = frozenset({"feat", "fix", "perf"})

#: The label that says "this one genuinely changes nothing for any reader".
OPT_OUT_LABEL = "no-changelog"

FRAGMENT_DIR = "changelog.d"
RELEASE_NOTES = "docs/release-notes.md"
PULL_URL = "https://github.com/Ironsail-llc/genus-os/pull/{pr}"

#: `README.md` documents the directory to the author; it is not a fragment.
NOT_A_FRAGMENT = frozenset({"README.md"})

FRAGMENT_NAME_RE = re.compile(r"^(?P<pr>\d+)\.(?P<audience>[a-z][a-z-]*)\.md$")

#: `feat(scope)!: subject` -> type `feat`, breaking.
TITLE_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<subject>.+)$"
)

#: Absolute paths that belong to one machine's user, not to the platform.
#: `/etc`, `/run`, `/var`, `/opt` and `/usr` are install locations every
#: instance has and are deliberately not listed.
PRIVATE_PATH_RE = re.compile(r"/(?:home|Users|root|mnt|media|tmp)/\w")

EMAIL_RE = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")
SAFE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "example.test"})

#: Same shape as `scripts/check_instance_leak.py`, including the boundary
#: guards that keep it out of UUIDs.
PHONE_RE = re.compile(r"(?<![\d.\-])\+?1?\s*[-.(]?\d{3}[-.)]\s*\d{3}[-.]?\d{4}(?![\d.\-])")

#: A sentence ends at `.`, `!` or `?` followed by whitespace and the start of
#: the next sentence. Requiring that next character rather than splitting on
#: the punctuation alone is what keeps `1.90.0` and `genus plugin sync` from
#: counting as three sentences each.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[`A-Z])")

MIN_SENTENCES = 2
MAX_SENTENCES = 4

#: Inline code and link targets are stripped before prose is measured: a
#: command is not a sentence, and a URL is not personal data.
INLINE_CODE_RE = re.compile(r"`[^`]*`")
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

_UNSET = object()


class AssembleError(RuntimeError):
    """The release cannot be assembled from the fragments as they stand."""


@dataclass(frozen=True)
class Fragment:
    """One pull request's note to one audience."""

    pr: int
    audience: str
    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class Finding:
    """One fragment that is not ready to ship."""

    name: str
    reason: str

    def __str__(self) -> str:
        return f"{FRAGMENT_DIR}/{self.name}: {self.reason}"


# ---------------------------------------------------------------------------
# Reading the directory
# ---------------------------------------------------------------------------


def fragment_dir(repo_root: Path) -> Path:
    return repo_root / FRAGMENT_DIR


def fragment_files(repo_root: Path) -> list[Path]:
    """Every file in `changelog.d/` that claims to be a fragment."""
    directory = fragment_dir(repo_root)
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name not in NOT_A_FRAGMENT and not path.name.startswith(".")
    )


def fragments(repo_root: Path) -> list[Fragment]:
    """The well-named fragments, oldest pull request first.

    A badly named file is not silently dropped -- `lint` reports it -- but it
    cannot be grouped or dated, so it is not a `Fragment`.
    """
    found: list[Fragment] = []
    for path in fragment_files(repo_root):
        match = FRAGMENT_NAME_RE.match(path.name)
        if match is None:
            continue
        found.append(
            Fragment(
                pr=int(match.group("pr")),
                audience=match.group("audience"),
                path=path,
                text=path.read_text(encoding="utf-8").strip(),
            )
        )
    return sorted(found, key=lambda fragment: (fragment.pr, fragment.audience))


# ---------------------------------------------------------------------------
# Pull-request titles and labels
# ---------------------------------------------------------------------------


def parse_labels(raw: str | None) -> list[str]:
    """Labels as GitHub hands them over: a JSON array, or comma-separated."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = []
        return [str(item).strip() for item in loaded if str(item).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def requires_fragment(title: str, labels: list[str]) -> bool:
    """True when this pull request must say what changed, and for whom."""
    if OPT_OUT_LABEL in labels:
        return False
    match = TITLE_RE.match(title.strip())
    if match is None:
        return False
    if match.group("breaking"):
        return True
    return match.group("type") in RELEASING_TYPES


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def _prose(text: str) -> str:
    """The fragment with code, fences and link targets taken out."""
    without_fences = FENCE_RE.sub(" ", text)
    without_links = LINK_RE.sub(lambda match: match.group(1), without_fences)
    without_code = INLINE_CODE_RE.sub(" ", without_links)
    return " ".join(without_code.split())


def sentences(text: str) -> list[str]:
    """The sentences of a fragment, for the two-to-four rule."""
    prose = _prose(text)
    if not prose:
        return []
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(prose) if part.strip()]


def _link_targets(text: str) -> list[str]:
    return [match.group(2) for match in LINK_RE.finditer(text)]


def _load_link_checker():
    """`scripts/check_doc_links.py`, loaded by path.

    The doc gates are scripts, not a package, and this one must run on a bare
    `python3` in CI -- before anything is pip-installed -- so it is imported
    the same way the tests import the gates themselves.
    """
    script = Path(__file__).resolve().parent / "check_doc_links.py"
    spec = importlib.util.spec_from_file_location("check_doc_links", script)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging fault
        raise RuntimeError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def known_paths(repo_root: Path) -> frozenset[str]:
    """Every tracked path, as `check_doc_links.py` resolves them."""
    return _load_link_checker().known_paths(repo_root)


def _check_links(text: str, known: frozenset[str]) -> list[str]:
    """Link failures, resolved from where the text will be PUBLISHED.

    A fragment lives in `changelog.d/` and is read in `docs/release-notes.md`,
    so `[Plugins](PLUGINS.md)` is correct and `[Plugins](docs/PLUGINS.md)` is
    not. Resolving from the fragment's own directory would bless exactly the
    links that break on the published page.
    """
    checker = _load_link_checker()
    return [
        finding.reason + f" ({finding.target})"
        for finding in checker.check_markdown(text, RELEASE_NOTES, known)
    ]


def published_pages(repo_root: Path) -> frozenset[str] | None:
    """The pages `mkdocs.yml` actually publishes, or None if there is no config.

    Parsed line by line rather than through a YAML loader: `mkdocs.yml` carries
    custom tags, this has to run on a bare `python3` with nothing installed,
    and `tests/test_docs_site.py` reads the same allowlist the same way.
    """
    config = repo_root / "mkdocs.yml"
    if not config.is_file():
        return None
    pages: set[str] = set()
    inside = False
    for line in config.read_text(encoding="utf-8").splitlines():
        if line.startswith("exclude_docs:"):
            inside = True
            continue
        if not inside:
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        entry = line.strip()
        if entry.startswith("!"):
            pages.add(entry.lstrip("!/"))
    return frozenset(pages)


def _unpublished_links(text: str, published: frozenset[str]) -> list[str]:
    """Link targets that resolve to a page the site does not serve.

    A fragment is published inside `docs/release-notes.md`, so a link to a page
    held out of `exclude_docs` renders as a dead href on the public site.
    mkdocs reports that at INFO and `--strict` does not fail on it, so nothing
    else in the pipeline catches it -- and the pages most likely to be linked
    are exactly the ones held back for carrying one deployment's data.
    """
    problems: list[str] = []
    for target in _link_targets(text):
        cleaned = target.strip().strip("<>")
        if not cleaned or cleaned.startswith(("http://", "https://", "mailto:", "#", "/")):
            continue
        page = cleaned.split("#", 1)[0].split("?", 1)[0]
        if not page or page.startswith("../"):
            problems.append(f"link '{target}' leaves the published docs tree")
            continue
        if page not in published:
            problems.append(
                f"link '{page}' is not a page the site publishes -- "
                f"the reader would follow it to a 404 (mkdocs.yml exclude_docs)"
            )
    return problems


def lint_fragment(
    path: Path,
    known: frozenset[str] | None,
    published: frozenset[str] | None = None,
) -> list[Finding]:
    """Every reason this one file is not ready to ship."""
    name = path.name
    findings: list[Finding] = []

    match = FRAGMENT_NAME_RE.match(name)
    if match is None:
        return [
            Finding(
                name,
                "name must be <PR>.<audience>.md, audience one of " + ", ".join(AUDIENCES),
            )
        ]

    audience = match.group("audience")
    if audience not in AUDIENCES:
        return [
            Finding(name, f"unknown audience '{audience}' -- use one of {', '.join(AUDIENCES)}")
        ]

    text = path.read_text(encoding="utf-8").strip()
    count = len(sentences(text))
    if count < MIN_SENTENCES or count > MAX_SENTENCES:
        findings.append(
            Finding(
                name,
                f"{count} sentence(s) -- a fragment is {MIN_SENTENCES} to "
                f"{MAX_SENTENCES}, written from the reader's side",
            )
        )

    private = PRIVATE_PATH_RE.search(text)
    if private is not None:
        findings.append(
            Finding(name, f"absolute path '{private.group(0)}...' -- name the setting, not a box")
        )

    findings.extend(
        Finding(name, f"personal data: address '{email.group(0)}' -- use agent@example.com")
        for email in EMAIL_RE.finditer(text)
        if email.group(1).lower() not in SAFE_EMAIL_DOMAINS
    )
    phone = PHONE_RE.search(text)
    if phone is not None:
        findings.append(
            Finding(name, f"personal data: '{phone.group(0).strip()}' looks like a phone number")
        )

    if known is not None:
        findings.extend(Finding(name, reason) for reason in _check_links(text, known))

    if published is not None:
        findings.extend(Finding(name, reason) for reason in _unpublished_links(text, published))

    if audience != "internal" and not _link_targets(text):
        findings.append(
            Finding(name, "names no doc page to read -- link the page the reader should open")
        )

    return findings


def lint(repo_root: Path, known: object = _UNSET) -> list[Finding]:
    """Every fragment in the tree that is not ready to ship.

    `known` defaults to the tracked paths of `repo_root`, so links are really
    resolved. Pass `None` to skip that one rule when there is no git tree to
    resolve against.
    """
    resolved: frozenset[str] | None
    if known is _UNSET:
        resolved = known_paths(repo_root)
    else:
        resolved = known  # type: ignore[assignment]

    findings: list[Finding] = []
    published = published_pages(repo_root)
    for path in fragment_files(repo_root):
        findings.extend(lint_fragment(path, resolved, published))
    return findings


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def check(
    repo_root: Path,
    pr: int,
    title: str,
    labels: list[str],
    known: frozenset[str] | None = None,
) -> list[str]:
    """What this pull request must fix before it can merge."""
    mine = [fragment for fragment in fragments(repo_root) if fragment.pr == pr]
    published = published_pages(repo_root)
    problems = [
        str(finding)
        for path in fragment_files(repo_root)
        if path.name.startswith(f"{pr}.")
        for finding in lint_fragment(path, known, published)
    ]

    if mine or not requires_fragment(title, labels):
        return problems

    return [
        *problems,
        f"'{title.strip()}' ships a change, but there is no release-note fragment for it. "
        f"Add {FRAGMENT_DIR}/{pr}.<audience>.md -- audience one of "
        f"{', '.join(AUDIENCES)} -- or label the pull request `{OPT_OUT_LABEL}`.",
    ]


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


def render_entry(fragment: Fragment) -> str:
    """One fragment exactly as the release notes will carry it.

    The body is reproduced **verbatim**, paragraphs and lists intact. An
    earlier version flattened it with `" ".join(text.split())`, which turned a
    two-item list into one run-on line -- and `preview` did not flatten, so the
    sticky comment promised the author something the release would not print.
    One function now answers for both, which is the only way those two can
    stay equal.

    The pull-request reference rides at the end of a one-paragraph note, where
    it reads as part of the sentence. Anything with a blank line or a list in
    it gets the reference as its own paragraph instead: after a bullet,
    markdown's lazy continuation would otherwise swallow it into the last item.
    """
    body = fragment.text.strip("\n")
    reference = f"([#{fragment.pr}]({PULL_URL.format(pr=fragment.pr)}))"
    if _is_one_paragraph(body):
        return f"{body} {reference}\n"
    return f"{body}\n\n{reference}\n"


def _is_one_paragraph(body: str) -> bool:
    """True for prose with no blank line and no block-level markdown in it."""
    if "\n\n" in body:
        return False
    return not any(
        line.lstrip().startswith(("-", "*", "+", ">", "#", "|")) or _is_numbered(line)
        for line in body.splitlines()
    )


def _is_numbered(line: str) -> bool:
    head = line.lstrip().split(".", 1)[0]
    return head.isdigit() and line.lstrip().startswith(f"{head}. ")


def published_audiences(items: list[Fragment]) -> list[str]:
    """The audiences in `items` that this page has a section for, in order."""
    return [
        audience
        for audience in AUDIENCES
        if audience in SECTION_TITLES and any(fragment.audience == audience for fragment in items)
    ]


def render_block(items: list[Fragment], version: str, date: str) -> str:
    """One dated version block, grouped by audience."""
    lines = [f"## {version} — {date}", ""]
    for audience in published_audiences(items):
        lines.append(f"### {SECTION_TITLES[audience]}")
        lines.append("")
        for fragment in items:
            if fragment.audience != audience:
                continue
            lines.extend(render_entry(fragment).rstrip("\n").split("\n"))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _insert(page: str, block: str) -> str:
    """Put the new block above every older one, below the page preamble."""
    lines = page.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return "".join(lines[:index]) + block + "\n" + "".join(lines[index:])
    return page.rstrip("\n") + "\n\n" + block


def assemble(
    repo_root: Path,
    version: str,
    date: str | None = None,
    known: frozenset[str] | None = None,
    delete: bool = True,
) -> str | None:
    """Fold every fragment into `docs/release-notes.md`; return the new block.

    Returns `None` and writes nothing when there is nothing to PUBLISH -- no
    fragments at all, or only `internal` ones -- so a release nobody can notice
    does not append a bare heading, and a re-run is a no-op rather than a
    duplicate.
    """
    items = fragments(repo_root)
    stray = [
        path for path in fragment_files(repo_root) if FRAGMENT_NAME_RE.match(path.name) is None
    ]
    if stray:
        raise AssembleError(
            "cannot assemble: "
            + ", ".join(f"{FRAGMENT_DIR}/{path.name}" for path in stray)
            + " is not named <PR>.<audience>.md"
        )
    if not items:
        return None

    findings = lint(repo_root, known=known)
    if findings:
        raise AssembleError(
            "cannot assemble, these fragments do not lint:\n"
            + "\n".join(f"  {finding}" for finding in findings)
        )

    # `internal` is the audience for "a release nobody can notice", and it has
    # no section on this page. A release whose every fragment is internal must
    # therefore write NO block -- the first version of this returned early only
    # on an empty directory, so such a release published a bare heading with
    # nothing under it. The fragments are still consumed: they belong to this
    # release, and carrying them forward would attribute them to the next one.
    if not published_audiences(items):
        if delete:
            for fragment in items:
                fragment.path.unlink()
        return None

    notes_path = repo_root / RELEASE_NOTES
    page = notes_path.read_text(encoding="utf-8") if notes_path.exists() else "# Release notes\n"
    if re.search(rf"^## {re.escape(version)}\b", page, re.MULTILINE):
        raise AssembleError(
            f"{RELEASE_NOTES} already has a {version} block -- a version is assembled once"
        )

    block = render_block(items, version, date or dt.datetime.now(tz=dt.UTC).date().isoformat())
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(_insert(page, block), encoding="utf-8")

    if delete:
        for fragment in items:
            fragment.path.unlink()
    return block


# ---------------------------------------------------------------------------
# preview: what the sticky pull-request comment says
# ---------------------------------------------------------------------------


def preview(repo_root: Path, pr: int, title: str, labels: list[str]) -> str:
    """The release-note half of the Release Preview comment."""
    mine = [fragment for fragment in fragments(repo_root) if fragment.pr == pr]
    if mine:
        lines = ["**Release notes.** This is what the next release will say:", ""]
        for fragment in mine:
            heading = SECTION_TITLES.get(fragment.audience, "Internal (recorded, not published)")
            lines.append(f"_{heading}_")
            lines.append("")
            # `render_entry`, not the raw file: the comment's whole claim is
            # that this is what will be published, so it must be produced by
            # the same function that publishes it. Every line is quoted --
            # including the blank ones, as a bare `>` -- because the workflow
            # pipes this through two heredocs and a fragment is
            # author-controlled text. An unquoted line that happens to be a
            # delimiter would end the heredoc and let the rest of the fragment
            # be read as further step outputs.
            lines.extend(
                f"> {line}" if line else ">" for line in render_entry(fragment).splitlines()
            )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    if requires_fragment(title, labels):
        return (
            f"**Release notes: no fragment.** Add `{FRAGMENT_DIR}/{pr}.<audience>.md` "
            f"— audience one of `{'`, `'.join(AUDIENCES)}` — two to four sentences written "
            f"from the reader's side, naming the page to open. If this really changes "
            f"nothing for any reader, label it `{OPT_OUT_LABEL}`.\n"
        )
    return ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _repo_root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    return Path(__file__).resolve().parents[1]


def _git_tracked(repo_root: Path) -> frozenset[str] | None:
    try:
        return known_paths(repo_root)
    except (subprocess.CalledProcessError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None, help="Repository root (default: this one)")
    commands = parser.add_subparsers(dest="command", required=True)

    check_parser = commands.add_parser("check", help="One pull request carries what it must")
    check_parser.add_argument("--pr", type=int, required=True)
    check_parser.add_argument("--title", required=True, help="The pull-request title")
    check_parser.add_argument("--labels", default="", help="JSON array or comma-separated")

    commands.add_parser("lint", help="Every fragment in the tree is shippable")

    assemble_parser = commands.add_parser("assemble", help="Fold fragments into the release notes")
    assemble_parser.add_argument("--version", required=True)
    assemble_parser.add_argument("--date", default=None, help="Default: today, UTC")

    preview_parser = commands.add_parser("preview", help="The sticky comment's release-note half")
    preview_parser.add_argument("--pr", type=int, required=True)
    preview_parser.add_argument("--title", required=True)
    preview_parser.add_argument("--labels", default="")

    args = parser.parse_args(argv)
    repo_root = _repo_root(args.repo_root)

    if args.command == "check":
        problems = check(
            repo_root,
            pr=args.pr,
            title=args.title,
            labels=parse_labels(args.labels),
            known=_git_tracked(repo_root),
        )
        if problems:
            print("RELEASE NOTES:")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print(f"changelog_fragments: #{args.pr} is covered.")
        return 0

    if args.command == "lint":
        findings = lint(repo_root)
        if findings:
            print("FRAGMENTS THAT WILL NOT SHIP:")
            for finding in findings:
                print(f"  {finding}")
            return 1
        print(f"changelog_fragments: {len(fragment_files(repo_root))} fragment(s) clean.")
        return 0

    if args.command == "assemble":
        try:
            block = assemble(
                repo_root,
                version=args.version,
                date=args.date,
                known=_git_tracked(repo_root),
            )
        except AssembleError as error:
            print(str(error))
            return 1
        if block is None:
            print(f"changelog_fragments: no fragments; {RELEASE_NOTES} unchanged.")
            return 0
        print(f"changelog_fragments: {RELEASE_NOTES} now opens with {args.version}.")
        return 0

    if args.command == "preview":
        print(preview(repo_root, pr=args.pr, title=args.title, labels=parse_labels(args.labels)))
        return 0

    return 2  # pragma: no cover - argparse requires a command


if __name__ == "__main__":
    sys.exit(main())
