"""A verdict about a file must be a verdict about the whole file.

`_read_text` returned the first 2,000,000 characters and every caller treated
them as the file. Measured by hostile review 2026-09-16 (C4):

- a **2.5 MB valid JSON array**, fields exactly as the spec listed, came back
  `mismatch: not valid JSON`;
- an **18.2 MB correctly-sorted TSV**, header exactly as the spec listed, came
  back `mismatch: not sorted`, naming row 153847 — the 2 MB cut had halved a
  line and the fragment sorted below its predecessor, so the row did not exist
  as described.

Under `enforce` each of those fails an otherwise-correct run. "I did not check"
and "it is wrong" are different sentences, and the cost of confusing them is
asymmetric: a silence loses a verdict the operator can still get by hand, a
false mismatch loses the run.
"""

from __future__ import annotations

import json

import pytest

from robothor.engine.deliverable_contract import (
    STATUS_MISMATCH,
    STATUS_OK,
    STATUS_UNCHECKED,
    JsonFieldsItem,
    SortItem,
    check_contract,
    extract_contract,
)

TABLE_SPEC = """\
Save the table to `/work/results/out.tsv`.

The TSV must use exactly the following header:

```text
Alpha\tBeta
```

- be sorted by `Alpha` ascending
"""

MANIFEST_SPEC = """\
Save the manifest to `/work/results/m.json`.

Save a JSON array. Each item must contain exactly these fields:

```json
[{"a": 1, "b": 2}]
```
"""


def _finding(spec, root, kind):
    found = [
        f for f in check_contract(extract_contract(spec), root).findings if isinstance(f.item, kind)
    ]
    assert len(found) == 1, found
    return found[0]


class TestASortedFileIsNotCalledUnsorted:
    def test_a_big_sorted_file_is_not_a_mismatch(self, tmp_path):
        """The measured case, in miniature: more rows than one slice holds,
        correctly sorted throughout."""
        results = tmp_path / "results"
        results.mkdir()
        rows = "".join(f"a{i:06d}\tx\n" for i in range(60_000))
        (results / "out.tsv").write_text("Alpha\tBeta\n" + rows, encoding="utf-8")
        assert _finding(TABLE_SPEC, tmp_path, SortItem).status == STATUS_OK

    def test_a_genuinely_unsorted_file_is_still_caught(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "out.tsv").write_text("Alpha\tBeta\nz\tx\na\tx\n", encoding="utf-8")
        assert _finding(TABLE_SPEC, tmp_path, SortItem).status == STATUS_MISMATCH

    def test_more_rows_than_the_cap_is_unchecked_not_a_verdict(self, tmp_path, monkeypatch):
        """Not a mismatch — and not nothing. Returning no finding at all left
        the one deliverable most likely to cross the cap as the one an operator
        could least tell had gone unread (re-review R3)."""
        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_STREAM_LINES", 10)
        results = tmp_path / "results"
        results.mkdir()
        rows = "".join(f"a{i:06d}\tx\n" for i in range(50))
        (results / "out.tsv").write_text("Alpha\tBeta\n" + rows, encoding="utf-8")
        report = check_contract(extract_contract(TABLE_SPEC), tmp_path)
        sort = [f for f in report.findings if isinstance(f.item, SortItem)]
        assert [f.status for f in sort] == [STATUS_UNCHECKED]
        assert report.satisfied, "an unread file is not a failing run"
        assert "NOT VERIFIED" in report.message


class TestAValidManifestIsNotCalledInvalid:
    def test_a_manifest_larger_than_the_text_cap_still_parses(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        payload = [{"a": "x" * 40, "b": i} for i in range(60_000)]
        blob = json.dumps(payload)
        (results / "m.json").write_text(blob, encoding="utf-8")
        assert len(blob) > 2_000_000, "the fixture must exceed the old text cap"
        assert _finding(MANIFEST_SPEC, tmp_path, JsonFieldsItem).status == STATUS_OK

    def test_beyond_the_hard_limit_it_declines_rather_than_lies(self, tmp_path, monkeypatch):
        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_JSON_BYTES", 50)
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}] * 20), encoding="utf-8")
        finding = _finding(MANIFEST_SPEC, tmp_path, JsonFieldsItem)
        assert finding.status == STATUS_UNCHECKED
        assert "too large to verify" in finding.reason

    def test_genuinely_broken_json_is_still_caught(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text("{not json", encoding="utf-8")
        assert _finding(MANIFEST_SPEC, tmp_path, JsonFieldsItem).status == STATUS_MISMATCH


class TestUncheckedIsNotAFailure:
    def test_the_report_is_satisfied_when_the_only_fault_is_a_silence(self, tmp_path, monkeypatch):
        """`enforce` fails a run on `failures`. A check that declined to read a
        file must not be counted among them."""
        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_JSON_BYTES", 50)
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}] * 20), encoding="utf-8")
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert report.satisfied

    def test_but_the_silence_is_still_said_out_loud(self, tmp_path, monkeypatch):
        """`satisfied` True and `message` empty made this invisible on every
        operator surface, against a source comment claiming the opposite
        (re-review R3)."""
        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_JSON_BYTES", 50)
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}] * 20), encoding="utf-8")
        report = check_contract(extract_contract(MANIFEST_SPEC), tmp_path)
        assert len(report.unchecked) == 1
        assert report.message.startswith("NOT VERIFIED:")
        assert "too large to verify" in report.message

    def test_the_operator_gets_a_row_of_its_own(self, tmp_path, monkeypatch):
        """`unchecked`, not `observed`: the evidence query has to separate "we
        looked and it was wrong" from "we could not look"."""
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts
        from robothor.engine.models import AgentRun, RunStatus

        rows: list[dict] = []
        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_JSON_BYTES", 50)
        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda **kw: rows.append(kw),
            raising=False,
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert",
            lambda **kw: rows.append({"alert": kw}),
        )
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}] * 20), encoding="utf-8")

        class _Session:
            originating_message = MANIFEST_SPEC
            messages: list[dict] = []

        session = _Session()
        session.run = run = AgentRun(agent_id="probe", status=RunStatus.COMPLETED)
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.COMPLETED, "not checking is not a fault of the run"
        assert [r.get("action") for r in rows] == ["unchecked"]
        assert not any("alert" in r for r in rows)

    def test_the_agent_is_told_while_it_can_still_act(self, tmp_path, monkeypatch):
        from robothor.engine.deliverable_contract import contract_checkin_note

        monkeypatch.setattr("robothor.engine.deliverable_check._MAX_JSON_BYTES", 50)
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}] * 20), encoding="utf-8")
        note = contract_checkin_note(MANIFEST_SPEC, tmp_path)
        assert note is not None
        assert "too large for the engine to verify" in note


class TestTheContainerComesFromTheProse:
    """I1 — the brief's named adversarial case, which the corpus sweep missed
    because the real spec happens to draw `[ { … } ]`."""

    SPEC = """\
Save the manifest to `/work/results/m.json`.

Save a JSON array. Each item must contain exactly these fields:

```json
{"a": 1, "b": 2}
```
"""

    def test_the_prose_wins_over_the_example(self):
        item = next(i for i in extract_contract(self.SPEC).items if isinstance(i, JsonFieldsItem))
        assert item.container == "array"
        assert item.fields == ("a", "b")

    def test_a_correct_array_file_passes(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "m.json").write_text(json.dumps([{"a": 1, "b": 2}]), encoding="utf-8")
        assert _finding(self.SPEC, tmp_path, JsonFieldsItem).status == STATUS_OK

    @pytest.mark.parametrize(
        "prose",
        ["Save a JSON object.", "The file is a JSON object."],
        ids=["save", "describes"],
    )
    def test_prose_naming_an_object_is_honoured_too(self, prose, tmp_path):
        spec = self.SPEC.replace("Save a JSON array.", prose)
        item = next(i for i in extract_contract(spec).items if isinstance(i, JsonFieldsItem))
        assert item.container == "object"

    def test_silence_falls_back_to_the_example(self):
        spec = self.SPEC.replace("Save a JSON array.", "Write the manifest.")
        item = next(i for i in extract_contract(spec).items if isinstance(i, JsonFieldsItem))
        assert item.container == "object"
