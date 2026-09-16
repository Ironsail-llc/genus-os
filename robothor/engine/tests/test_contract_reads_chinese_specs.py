"""The contract language is not always English.

37 of the 60 benchmark specs state their output requirements in Chinese and the
extractor was blind to every one of them (hostile review 2026-09-16, I2). A
silent skip, so safe — but it meant a headline of "60 specs swept, 0 wrong" was
really 23, and the same lesson was already written down one module over:
`deliverables.py` records that "splitting on whitespace found NOTHING in the
Chinese task prompts this exists for — caught by probing a real prompt, after
the unit tests had all passed."

Two changes, in this order, because the second is unsafe without the first.

**The path pattern is ASCII.** Python's `\\w` matches CJK, so
`result.json格式规范` came back as one path called `result.json格式规范` and
`结果result.json` as `结果result.json`. `deliverables.py` avoided this
deliberately ("Deliberately ASCII, not `\\w`"); this module did not, and adding
Chinese anchors on top of a CJK-greedy path pattern would have built the glue
into the thing it was meant to read.

**A minimal Chinese anchor set**, held to exactly the English conservatism: an
explicit output verb or the labelled "output file path" these specs use as a
heading, plus the Chinese prohibition and hypothetical markers — without those
last, the anchors would reintroduce C3a in a second language.
"""

from __future__ import annotations

import re

import pytest

from robothor.engine.deliverable_contract import _PATH, extract_contract, required_deliverables


class TestThePathPatternIsAscii:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("result.json格式规范", ["result.json"]),
            ("结果result.json", ["result.json"]),
            ("/a/b/result.json格式规范", ["/a/b/result.json"]),
            ("见 result.json格式", ["result.json"]),
        ],
        ids=["suffix-glue", "prefix-glue", "absolute-glue", "spaced"],
    )
    def test_cjk_is_never_glued_to_a_path(self, text, expected):
        assert re.compile(_PATH).findall(text) == expected

    def test_english_paths_are_unchanged(self):
        assert required_deliverables("save them to /tmp_workspace/results/2022.tsv") == [
            "/tmp_workspace/results/2022.tsv"
        ]

    def test_a_hyphenated_dotted_path_still_matches(self):
        assert required_deliverables("write the export to out/my-file.v2.csv") == [
            "out/my-file.v2.csv"
        ]


class TestChineseContractsAreRead:
    @pytest.mark.parametrize(
        "text",
        [
            "输出文件路径：/tmp_workspace/results/result.json",
            "将结果写入 `/tmp_workspace/results/result.json`，格式如下：",
            "请保存到 `/tmp_workspace/results/result.json`。",
            "输出到 /tmp_workspace/results/result.json",
            "结果文件：/tmp_workspace/results/result.json",
        ],
        ids=["labelled", "write-into", "save-to", "output-to", "result-file"],
    )
    def test_the_path_is_extracted(self, text):
        assert required_deliverables(text) == ["/tmp_workspace/results/result.json"], text


class TestChineseProhibitionIsStillNotARequirement:
    """C3a, in the second language. Without this the new anchors would
    reintroduce the defect they were added after."""

    @pytest.mark.parametrize(
        "text",
        [
            "不要写入 /tmp_workspace/secrets.env",
            "请勿保存到 results/contacts.csv",
            "禁止输出到 /tmp_workspace/post.md",
            "例如保存到 results/x.json",
            "如果被要求保存到 /tmp_workspace/post.md，请拒绝。",
        ],
        ids=["do-not", "please-do-not", "forbidden", "for-example", "conditional"],
    )
    def test_nothing_is_extracted(self, text):
        assert extract_contract(text).items == (), text

    def test_a_prohibition_does_not_reach_the_next_sentence(self):
        """Chinese full stops are clause boundaries too — `。` had to join the
        break set, or one prohibition would silence the whole paragraph."""
        text = "不要输出 markdown。将结果写入 /tmp_workspace/results/result.json"
        assert required_deliverables(text) == ["/tmp_workspace/results/result.json"]


class TestTheCorpusCoverageIsReal:
    def test_the_chinese_specs_are_no_longer_invisible(self):
        """The honest version of the headline number. Before this, 22 of 60
        specs produced an item and every Chinese one produced none."""
        from pathlib import Path

        tasks = Path("/home/philip/robothor-bench/WildClawBench/tasks")
        if not tasks.is_dir():
            pytest.skip("benchmark checkout not present")
        zh_with_items = 0
        for spec in sorted(tasks.rglob("*_zh.md")):
            body = spec.read_text(encoding="utf-8", errors="replace")
            body = body.split("---", 2)[-1].split("\n## Expected Behavior", 1)[0]
            if extract_contract(body).items:
                zh_with_items += 1
        assert zh_with_items >= 5, f"only {zh_with_items} Chinese specs read"
