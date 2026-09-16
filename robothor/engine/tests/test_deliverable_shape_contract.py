"""A deliverable contract is a SHAPE, not only a path.

Measured 2026-09-16 on three Productivity tasks of the public agent benchmark,
run like-for-like against a competing harness on the same model and the same
graders: the competitor scored 86 / 91 / 49, this engine scored 0 / 0 / 0. In
all three the agent did the research and then ignored the exact output contract
-- wrote its own TSV columns, wrote the manifest to its own path, renamed the
section headings it had been told not to rename. The path-only contract this
module shipped with saw a file at the right place and said nothing.

So the fixtures here are shapes, and every one of them is a generic paraphrase:
platform tests carry no benchmark text and no personal data. The failing
workspaces are reproduced by their SHAPE (wrong header, absent directory,
renamed headings), which is the only part of them this code can see.

Extraction stays conservative for the reason the module docstring gives: a
control that nags about a contract the task never stated gets muted, and a
muted control protects nothing. Hence five negatives for every positive.
"""

from __future__ import annotations

import json

import pytest

from robothor.engine.deliverable_contract import (
    ExactSetItem,
    HeaderItem,
    JsonFieldsItem,
    PathItem,
    SectionsItem,
    SortItem,
    check_contract,
    extract_contract,
)

# ─── Fixtures: generic paraphrases of the three shapes that scored 0 ───

#: A tabular deliverable with an exact header, an exact delimiter and a sort.
#: Note the fenced header is separated by RUNS OF SPACES, not tabs -- that is
#: how a spec renders a tab-delimited header in markdown, and splitting it on
#: tabs would yield one column named after the whole line.
TABLE_SPEC = """\
Please compile the matching talks and save them to:

- `/work/results/talks.tsv`

### Output Requirements

You must create the following outputs under `/work/results/`:

- `talks.tsv`
- one or more source `.tex` files

Do not create any other files or directories under `results/`.

#### TSV Format

The TSV file must:

- be UTF-8 encoded
- contain exactly one header row
- use tab (`\\t`) as the delimiter
- use exactly the following header:

```text
Track  Title   Speakers    Summary   Speaker links   Commit id
```

- contain exactly one row per talk
- be sorted by `Track` ascending, then by `Title` ascending
"""

#: An exact output SET plus a JSON array whose items have exact fields.
MANIFEST_SPEC = """\
### Output Requirements

You must create exactly the following outputs under `/work/results/`:

- `talk_manifest.json`
- `renamed_slides/`
- `citations/`

Do not create any other files or directories.

#### `talk_manifest.json`

Save a JSON array. Each item must contain exactly these fields:

```json
[
  {
    "original_filename": "messy_name_01.pdf",
    "talk_id": "0000.00000",
    "title": "Exact Official Talk Title",
    "renamed_filename": "Exact Official Talk Title.pdf",
    "figure_count": 12
  }
]
```
"""

#: A prose document with headings the agent is told not to rename.
DIGEST_SPEC = """\
Save everything to `/work/results/digest.md` using exactly the following
structure (do not rename these section headings):

```markdown
# Daily Digest

### Classification
#### Tooling
- **Talk Title** (id)

#### Infrastructure
- ...

### Metadata Audit

### Recommendations
#### Talks of Interest
```
"""


def _items(text: str, kind: type) -> list:
    return [i for i in extract_contract(text).items if isinstance(i, kind)]


def _one(text: str, kind: type):
    found = _items(text, kind)
    assert len(found) == 1, f"expected exactly one {kind.__name__}, got {found}"
    return found[0]


# ─── Extraction: the shapes a spec states explicitly ───


class TestHeaderExtraction:
    def test_the_exact_header_becomes_columns(self):
        item = _one(TABLE_SPEC, HeaderItem)
        assert item.columns == (
            "Track",
            "Title",
            "Speakers",
            "Summary",
            "Speaker links",
            "Commit id",
        )

    def test_a_space_separated_fence_is_not_read_as_one_column(self):
        """Multi-word column names survive. `Speaker links` is one column, and
        splitting the fence on single spaces would make it two."""
        assert "Speaker links" in _one(TABLE_SPEC, HeaderItem).columns

    def test_the_delimiter_comes_from_the_spec(self):
        assert _one(TABLE_SPEC, HeaderItem).delimiter == "\t"

    def test_the_header_is_attached_to_the_named_file(self):
        assert _one(TABLE_SPEC, HeaderItem).path == "/work/results/talks.tsv"

    def test_it_carries_the_sentence_it_came_from(self):
        """A nag that cannot quote the spec is an assertion; one that can is
        evidence. The agent has the spec in its context and can check us."""
        assert "exactly the following header" in _one(TABLE_SPEC, HeaderItem).evidence

    def test_a_comma_delimited_columns_line(self):
        text = "Write the export to `out/rows.csv`. The columns are: Name, Email, Joined."
        item = _one(text, HeaderItem)
        assert item.columns == ("Name", "Email", "Joined")
        assert item.delimiter == ","


class TestExactSetExtraction:
    def test_a_prose_bullet_stops_the_set_being_exact(self):
        """`one or more source .tex files` is not a filename. A set we cannot
        enumerate cannot forbid extras -- claiming otherwise would fail a run
        for a file the spec allowed."""
        item = _one(TABLE_SPEC, ExactSetItem)
        assert item.names == ("talks.tsv",)
        assert item.forbid_extra is False

    def test_exactly_the_following_forbids_extras(self):
        item = _one(MANIFEST_SPEC, ExactSetItem)
        assert item.directory == "/work/results/"
        assert item.names == ("talk_manifest.json", "renamed_slides/", "citations/")
        assert item.forbid_extra is True


class TestJsonFieldsExtraction:
    def test_the_fields_come_from_the_fenced_example(self):
        item = _one(MANIFEST_SPEC, JsonFieldsItem)
        assert item.fields == (
            "original_filename",
            "talk_id",
            "title",
            "renamed_filename",
            "figure_count",
        )

    def test_a_json_array_is_recorded_as_an_array(self):
        assert _one(MANIFEST_SPEC, JsonFieldsItem).container == "array"

    def test_the_bare_filename_is_joined_to_the_output_directory(self):
        """The spec names `talk_manifest.json` under a heading, and the
        directory three paragraphs earlier. A checker handed the bare name
        would look in the workspace root and pass on the wrong file."""
        assert _one(MANIFEST_SPEC, JsonFieldsItem).path == "/work/results/talk_manifest.json"


class TestSectionsExtraction:
    def test_the_headings_are_taken_from_the_fenced_structure(self):
        item = _one(DIGEST_SPEC, SectionsItem)
        assert item.headings == (
            "Daily Digest",
            "Classification",
            "Tooling",
            "Infrastructure",
            "Metadata Audit",
            "Recommendations",
            "Talks of Interest",
        )

    def test_it_is_attached_to_the_named_document(self):
        assert _one(DIGEST_SPEC, SectionsItem).path == "/work/results/digest.md"


class TestSortExtraction:
    def test_both_sort_keys_are_read(self):
        assert _one(TABLE_SPEC, SortItem).keys == ("Track", "Title")

    def test_a_sort_without_a_known_header_is_not_extracted(self):
        """Sortedness is only checkable against named columns. Without a
        header item there is nothing to resolve `Track` against, so claiming
        the contract would produce a verdict we cannot compute."""
        assert _items("Rows must be sorted by `Track` ascending.", SortItem) == []


class TestPathsStillExtracted:
    def test_the_original_path_contract_survives(self):
        paths = [i.path for i in _items(TABLE_SPEC, PathItem)]
        assert "/work/results/talks.tsv" in paths


# ─── Extraction: what must NOT be extracted ───


class TestNoFalsePositives:
    """Five shapes that mention files and promise nothing."""

    def test_prose_mentioning_a_file(self):
        assert extract_contract("The talks.tsv format has one row per talk.").items == ()

    def test_a_url(self):
        assert extract_contract("Fetch https://example.com/results/talks.tsv first.").items == ()

    def test_a_read_instruction(self):
        assert extract_contract("Read the records from data/input.csv and summarise.").items == ()

    def test_a_bare_extension(self):
        assert extract_contract("Prefer .tsv over .csv formatting.").items == ()

    def test_an_example_that_is_not_a_requirement(self):
        text = (
            "For example, a header like `Name, Email` is acceptable, and some "
            "teams sort by `Name` ascending. Use whatever suits the data."
        )
        assert extract_contract(text).items == ()

    def test_empty_and_none(self):
        assert extract_contract("").items == ()
        assert extract_contract(None).items == ()


# ─── Checking: the three shapes that scored 0 ───


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestHeaderChecking:
    """Workspace shape 1: the right path, the agent's own columns."""

    def test_the_reason_names_both_headers(self, tmp_path):
        _write(tmp_path, "results/talks.tsv", "Title\tSpeakers\tVenue\tYear\tSummary\nx\n")
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        reason = report.message
        assert "Title Speakers Venue Year Summary" in reason
        assert "Track Title Speakers Summary Speaker links Commit id" in reason
        assert not report.satisfied

    def test_a_fence_that_renders_a_tab_as_one_space_still_compares(self, tmp_path):
        """The measured task drew its six-column header with runs of one to
        four spaces, so no split of that line is reliable. Comparing the two
        headers with their whitespace collapsed is: a correct file passes
        however the spec drew the requirement."""
        ambiguous = TABLE_SPEC.replace("Summary   Speaker links", "Summary Speaker links")
        _write(
            tmp_path,
            "results/talks.tsv",
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\n",
        )
        header = [
            f
            for f in check_contract(extract_contract(ambiguous), tmp_path).findings
            if isinstance(f.item, HeaderItem)
        ]
        assert [f.status for f in header] == ["ok"]

    def test_a_single_column_file_cannot_satisfy_a_delimited_header(self, tmp_path):
        """Collapsing whitespace is what makes an ambiguous fence checkable; on
        its own it would also let a file with no delimiter at all pass."""
        _write(
            tmp_path,
            "results/talks.tsv",
            "Track Title Speakers Summary Speaker links Commit id\n",
        )
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        assert "does not use a tab" in report.message

    def test_the_reason_names_the_dropped_columns(self, tmp_path):
        """Two required fields were dropped. Naming them is the difference
        between a verdict and a remedy."""
        _write(tmp_path, "results/talks.tsv", "Title\tSpeakers\tVenue\tYear\tSummary\n")
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        assert "Speaker links" in report.message
        assert "Commit id" in report.message

    def test_an_exact_header_passes(self, tmp_path):
        _write(
            tmp_path,
            "results/talks.tsv",
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\n",
        )
        findings = check_contract(extract_contract(TABLE_SPEC), tmp_path).findings
        header = [f for f in findings if isinstance(f.item, HeaderItem)]
        assert [f.status for f in header] == ["ok"]

    @pytest.mark.parametrize(
        "raw",
        [
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\r\n",
            "﻿Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\n",
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id  \n",
            " Track \t Title \t Speakers \t Summary \t Speaker links \t Commit id \n",
        ],
        ids=["crlf", "bom", "trailing-spaces", "padded-cells"],
    )
    def test_transport_damage_is_not_a_contract_breach(self, tmp_path, raw):
        """A CRLF, a BOM or a padded cell is how the file travelled, not what
        the agent chose to call its columns. Failing a run for those is the
        false-positive class that gets a control muted."""
        _write(tmp_path, "results/talks.tsv", raw)
        header = [
            f
            for f in check_contract(extract_contract(TABLE_SPEC), tmp_path).findings
            if isinstance(f.item, HeaderItem)
        ]
        assert [f.status for f in header] == ["ok"], raw


class TestExactSetChecking:
    """Workspace shape 2: the output directory is empty, the work is elsewhere."""

    def test_the_reason_names_every_absent_member(self, tmp_path):
        (tmp_path / "results").mkdir()
        _write(tmp_path, "output.json", "[]")
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        for name in ("talk_manifest.json", "renamed_slides/", "citations/"):
            assert name in report.message

    def test_an_extra_file_is_named_when_the_set_is_exact(self, tmp_path):
        _write(tmp_path, "results/talk_manifest.json", "[]")
        (tmp_path / "results" / "renamed_slides").mkdir()
        (tmp_path / "results" / "citations").mkdir()
        _write(tmp_path, "results/scratch.txt", "notes")
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert "scratch.txt" in report.message

    def test_an_extra_file_is_allowed_when_the_set_is_not_exact(self, tmp_path):
        _write(tmp_path, "results/talks.tsv", "Track\tTitle\tSpeakers\tSummary\t")
        _write(tmp_path, "results/paper.tex", "\\documentclass{article}")
        exact = [
            f
            for f in check_contract(extract_contract(TABLE_SPEC), tmp_path).findings
            if isinstance(f.item, ExactSetItem)
        ]
        assert [f.status for f in exact] == ["ok"]


class TestJsonFieldsChecking:
    def test_an_absent_manifest_points_at_what_was_written_instead(self, tmp_path):
        """The run wrote its answer to the workspace root. Saying only 'not
        found' sends the agent looking for work it already did."""
        (tmp_path / "results").mkdir()
        _write(tmp_path, "output.json", json.dumps([{"title": "t"}]))
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert "talk_manifest.json" in report.message
        assert "output.json" in report.message

    def test_an_object_where_an_array_was_required(self, tmp_path):
        _write(tmp_path, "results/talk_manifest.json", json.dumps({"talks": []}))
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert "JSON object" in report.message
        assert "array" in report.message

    def test_missing_and_unexpected_fields_are_both_named(self, tmp_path):
        _write(
            tmp_path,
            "results/talk_manifest.json",
            json.dumps([{"original_filename": "a.pdf", "title": "t", "pages": 3}]),
        )
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert "talk_id" in report.message
        assert "pages" in report.message

    def test_the_exact_fields_pass(self, tmp_path):
        _write(
            tmp_path,
            "results/talk_manifest.json",
            json.dumps(
                [
                    {
                        "original_filename": "a.pdf",
                        "talk_id": "0000.1",
                        "title": "t",
                        "renamed_filename": "t.pdf",
                        "figure_count": 1,
                    }
                ]
            ),
        )
        fields = [
            f
            for f in check_contract(extract_contract(MANIFEST_SPEC), tmp_path).findings
            if isinstance(f.item, JsonFieldsItem)
        ]
        assert [f.status for f in fields] == ["ok"]

    def test_unparseable_json_says_so(self, tmp_path):
        _write(tmp_path, "results/talk_manifest.json", "{not json")
        assert "not valid JSON" in check_contract(extract_contract(MANIFEST_SPEC), tmp_path).message


class TestSectionsChecking:
    """Workspace shape 3: the right path, the agent's own headings."""

    def test_renamed_headings_are_named(self, tmp_path):
        _write(
            tmp_path,
            "results/digest.md",
            "# Daily Digest — today\n\n## Full Classification of All Talks\n\n### 1. Tooling (4)\n",
        )
        report = check_contract(extract_contract(DIGEST_SPEC), tmp_path)
        assert "Classification" in report.message
        assert "Talks of Interest" in report.message

    def test_a_trailing_colon_is_not_a_rename(self, tmp_path):
        body = "\n".join(
            f"# {h}:"
            for h in (
                "Daily Digest",
                "Classification",
                "Tooling",
                "Infrastructure",
                "Metadata Audit",
                "Recommendations",
                "Talks of Interest",
            )
        )
        _write(tmp_path, "results/digest.md", body + "\n")
        sections = [
            f
            for f in check_contract(extract_contract(DIGEST_SPEC), tmp_path).findings
            if isinstance(f.item, SectionsItem)
        ]
        assert [f.status for f in sections] == ["ok"]

    def test_a_different_heading_level_is_not_a_rename(self, tmp_path):
        """The spec said do not rename the headings, not do not re-nest them.
        Failing a run over a `###` where the fence showed `####` is a verdict
        about markdown, not about the deliverable."""
        body = "\n".join(
            f"##### {h}"
            for h in (
                "Daily Digest",
                "Classification",
                "Tooling",
                "Infrastructure",
                "Metadata Audit",
                "Recommendations",
                "Talks of Interest",
            )
        )
        _write(tmp_path, "results/digest.md", body + "\n")
        sections = [
            f
            for f in check_contract(extract_contract(DIGEST_SPEC), tmp_path).findings
            if isinstance(f.item, SectionsItem)
        ]
        assert [f.status for f in sections] == ["ok"]


class TestSortChecking:
    def test_unsorted_rows_are_reported(self, tmp_path):
        _write(
            tmp_path,
            "results/talks.tsv",
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\n"
            "B\tz\ts\tx\tl\tc\n"
            "A\ta\ts\tx\tl\tc\n",
        )
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        assert "sorted" in report.message

    def test_sorted_rows_pass(self, tmp_path):
        _write(
            tmp_path,
            "results/talks.tsv",
            "Track\tTitle\tSpeakers\tSummary\tSpeaker links\tCommit id\n"
            "A\ta\ts\tx\tl\tc\n"
            "B\tz\ts\tx\tl\tc\n",
        )
        sort = [
            f
            for f in check_contract(extract_contract(TABLE_SPEC), tmp_path).findings
            if isinstance(f.item, SortItem)
        ]
        assert [f.status for f in sort] == ["ok"]

    def test_sort_is_silent_when_the_header_is_wrong(self, tmp_path):
        """The header item already carries that fault. Reporting `Track` as an
        unknown column on top of it is the same finding twice."""
        _write(tmp_path, "results/talks.tsv", "Title\tSpeakers\n")
        sort = [
            f
            for f in check_contract(extract_contract(TABLE_SPEC), tmp_path).findings
            if isinstance(f.item, SortItem)
        ]
        assert sort == []


class TestNothingProducedAtAll:
    """The run that writes nothing is not the interesting case, but it is the
    one an enforce rung must not crash on."""

    def test_every_item_reports_missing(self, tmp_path):
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        assert not report.satisfied
        assert {f.status for f in report.failures} == {"missing"}

    def test_the_reason_still_names_the_required_header(self, tmp_path):
        """ "File not found" alone tells an agent nothing it did not know. It
        has to leave with the requirement, not only the verdict."""
        message = check_contract(extract_contract(TABLE_SPEC), tmp_path).message
        assert "Track Title Speakers Summary Speaker links Commit id" in message

    def test_an_absent_output_directory_is_named(self, tmp_path):
        message = check_contract(extract_contract(MANIFEST_SPEC), tmp_path).message
        assert "results/" in message
        assert "talk_manifest.json" in message


class TestItCannotBeHungByTaskText:
    """Extraction runs over untrusted task text before the agent takes a step,
    and again at every check-in. Three regex attempts preceded the shipped path
    pattern, each flagged by CodeQL for polynomial backtracking."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "/" * 4000,
            "a." * 4000,
            "-/" * 4000,
            "You must create the following outputs under `" + ("a/" * 2000),
            "use exactly the following header:\n\n```text\n" + ("x " * 4000),
            "sorted by " + ("a" * 4000),
        ],
        ids=["slashes", "dots", "dashes", "unclosed-dir", "unclosed-fence", "unclosed-sort"],
    )
    def test_pathological_text_returns_promptly(self, hostile):
        import time

        started = time.monotonic()
        extract_contract(hostile)
        assert time.monotonic() - started < 2.0


class TestContainment:
    def test_a_path_outside_the_workspace_is_never_touched(self, tmp_path):
        """Task text is untrusted in any deployment where someone else can file
        a task, and these paths reach the filesystem."""
        text = "Save the export to `/work/results/../../etc/shadow.csv`"
        report = check_contract(extract_contract(text), tmp_path / "ws")
        assert report.findings == ()

    def test_no_contract_is_an_empty_report(self, tmp_path):
        report = check_contract(extract_contract("Summarise the inbox."), tmp_path)
        assert report.satisfied
        assert report.message == ""
