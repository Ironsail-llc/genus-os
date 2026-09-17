"""A loop that varies its arguments is still a loop.

The repeat guard (#571/#575) keys on a canonical hash of the arguments, so it
catches a call repeated EXACTLY. The runs that burn their whole budget do not
repeat exactly. Three shapes, taken verbatim from the recorded transcripts:

* `02_Code_Intelligence` `sam3_debug` (2026-09-15, 273 requests, 1191s, score
  0.0) re-ran `python test_sam3.py` after each incremental `pip install`, and
  read ONE module thirteen times at eight different `offset`/`limit` windows —
  four of them duplicates of an earlier window, two of those differing from it
  only in that the numbers arrived as strings (`'130'`) rather than ints. The
  canonical key sees `{"limit": 130}` and `{"limit": "130"}` as two calls.
* the same run asked one question — where are scores and boxes computed —
  twenty times as `search_files` patterns with overlapping regex vocabulary.
* `link_a_pix_color_zh` rewrote the same grid-cropping script as `rescan.py`,
  `rescan2.py`, `rescan3.py`, `cluster.py`, `cluster2.py`, `cluster_all.py`…

What they have in common is not the arguments. It is that the RESULTS stopped
carrying anything the run did not already have. So the signature is coarse —
the command head, the path, the domain, the query stem — and the trigger is
the absence of new information, which is what makes a coarse signature safe:
ten different files read once each are ten new results and never escalate.
"""

from __future__ import annotations

from typing import Any


def _tracker(**kw: Any) -> Any:
    from robothor.engine.repeat_variants import VariantTracker

    return VariantTracker(**kw)


def _exec(stdout: str = "", **kw: Any) -> dict[str, Any]:
    return {"stdout": stdout, "stderr": "", "exit_code": 0, **kw}


# ── The signature ───────────────────────────────────────────────────────────


class TestTheSignature:
    def test_exec_is_keyed_on_the_command_head(self) -> None:
        from robothor.engine.repeat_variants import signature

        a = signature(
            "exec", {"command": "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -40"}
        )
        b = signature(
            "exec", {"command": "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -5"}
        )
        assert a == b

    def test_a_different_command_is_a_different_family(self) -> None:
        from robothor.engine.repeat_variants import signature

        a = signature("exec", {"command": "python test_sam3.py"})
        b = signature("exec", {"command": "pip install --quiet einops"})
        assert a != b

    def test_a_read_is_keyed_on_the_path_not_the_window(self) -> None:
        from robothor.engine.repeat_variants import signature

        path = "/tmp_workspace/sam3/sam3/model/sam3_image.py"
        assert signature("read_file", {"path": path, "offset": 439, "limit": 90}) == signature(
            "read_file", {"path": path, "offset": 165, "limit": 200}
        )

    def test_two_different_files_are_two_families(self) -> None:
        from robothor.engine.repeat_variants import signature

        assert signature("read_file", {"path": "/w/a.py"}) != signature(
            "read_file", {"path": "/w/b.py"}
        )

    def test_a_fetch_is_keyed_on_domain_and_path_not_the_query_string(self) -> None:
        from robothor.engine.repeat_variants import signature

        a = signature("web_fetch", {"url": "https://example.test/papers/list?page=1"})
        b = signature("web_fetch", {"url": "https://example.test/papers/list?page=2"})
        assert a == b and a is not None

    def test_an_unguarded_tool_has_no_signature(self) -> None:
        from robothor.engine.repeat_variants import signature

        assert signature("write_file", {"path": "/w/out.md"}) is None


class TestQuerySimilarity:
    def test_a_reworded_query_lands_in_the_same_family(self) -> None:
        from robothor.engine.repeat_variants import same_family, signature

        a = signature("web_search", {"query": "how to fix the pytest assertion failure"})
        b = signature("web_search", {"query": "pytest assertion failure fix"})
        assert same_family("web_search", a, b)

    def test_a_genuinely_different_query_does_not(self) -> None:
        from robothor.engine.repeat_variants import same_family, signature

        a = signature("web_search", {"query": "pytest assertion failure fix"})
        b = signature("web_search", {"query": "link-a-pix puzzle rules path length"})
        assert not same_family("web_search", a, b)

    def test_overlapping_search_patterns_are_one_question(self) -> None:
        """Ten regexes, one question — `sam3_debug`'s twenty `search_files`."""
        from robothor.engine.repeat_variants import same_family, signature

        a = signature(
            "search_files",
            {"path": "/w/model", "pattern": "class_embed|pred_logits|presence|forward_grounding"},
        )
        b = signature(
            "search_files",
            {"path": "/w/model", "pattern": "forward_grounding|presence|class_embed|bbox_embed"},
        )
        assert same_family("search_files", a, b)

    def test_a_synonym_is_a_different_question_and_this_is_a_known_limit(self) -> None:
        """Pinned as a LIMIT, not as a feature.

        Hostile review 2026-09-17, finding 4: five realistic rewordings of one
        question produced four families and nothing was said. Jaccard >= 0.6
        over a two- or three-token query means one synonym breaks the family —
        `{computed, score}` vs `{calculated, score}` is 0.33. Lowering the
        threshold would merge unrelated searches, which costs a capability;
        so the shape is caught for near-identical rewordings only, and a sweep
        that counts ZERO variant rows on `web_search` must read that as "aimed
        at nothing", never as "the loop shape is gone".
        """
        from robothor.engine.repeat_variants import same_family, signature

        a = signature("web_search", {"query": "where is the score computed"})
        b = signature("web_search", {"query": "where is the score calculated"})
        assert not same_family("web_search", a, b)

    def test_the_stemmer_drops_one_plural_and_not_a_run_of_esses(self) -> None:
        """`str.rstrip("s")` made `class` -> `cla` and `status` -> `statu`."""
        from robothor.engine.repeat_variants import _stem

        assert [_stem(w) for w in ("class", "process", "status", "analysis")] == [
            "class",
            "process",
            "status",
            "analysis",
        ]
        assert [_stem(w) for w in ("papers", "files")] == ["paper", "file"]

    def test_a_path_shaped_signature_needs_an_exact_match(self) -> None:
        """No fuzz where the argument is a name: `/w/a.py` is not `/w/b.py`."""
        from robothor.engine.repeat_variants import same_family, signature

        a = signature("read_file", {"path": "/w/deep/module_one.py"})
        b = signature("read_file", {"path": "/w/deep/module_two.py"})
        assert not same_family("read_file", a, b)


# ── The ladder ──────────────────────────────────────────────────────────────


class TestTheLadder:
    def test_nothing_is_said_while_the_results_keep_changing(self) -> None:
        """One family — only the flags vary — and every result is new."""
        tracker = _tracker()
        for i in range(10):
            args = {"command": f"python test_sam3.py --seed={i}"}
            assert tracker.verdict("exec", args) is None
            tracker.observe("exec", args, _exec(stdout=f"case {i} failed on line {i * 7}"))

    def test_thirty_different_files_read_once_each_never_escalate(self) -> None:
        """The hostile case. A coarse signature is only safe if this holds."""
        tracker = _tracker()
        for i in range(30):
            args = {"path": f"/w/module_{i}.py"}
            assert tracker.verdict("read_file", args) is None
            tracker.observe("read_file", args, {"content": f"# module {i}\nvalue = {i}\n"})

    def test_a_note_after_three_calls_that_brought_nothing_new(self) -> None:
        tracker = _tracker()
        for i in range(3):
            args = {"command": f"cd /w && python test_sam3.py 2>&1 | tail -{40 + i}"}
            tracker.observe("exec", args, _exec(stdout="AssertionError: shapes do not match"))
        verdict = tracker.verdict("exec", {"command": "cd /w && python test_sam3.py --tb=short"})
        assert verdict is not None
        assert verdict.action == "noted"
        assert "python" in verdict.note

    def test_the_note_is_said_once_not_on_every_later_call(self) -> None:
        tracker = _tracker()
        args = {"command": "python test_sam3.py"}
        for _ in range(3):
            tracker.observe("exec", args, _exec(stdout="AssertionError"))
        first = tracker.verdict("exec", {"command": "python test_sam3.py -v"})
        second = tracker.verdict("exec", {"command": "python test_sam3.py -q"})
        assert first is not None and first.action == "noted"
        assert second is None or second.action != "noted"

    def test_a_fifth_stale_exec_is_refused(self) -> None:
        tracker = _tracker()
        for i in range(5):
            tracker.observe(
                "exec",
                {"command": f"python test_sam3.py --flag{i}"},
                _exec(stdout="AssertionError: shapes do not match"),
            )
        verdict = tracker.verdict("exec", {"command": "python test_sam3.py --flag9"})
        assert verdict is not None and verdict.action == "refused"
        assert verdict.result is not None
        assert "error" not in verdict.result, "a refusal is a redirection, not a fault"

    def test_new_information_resets_the_streak(self) -> None:
        tracker = _tracker()
        for i in range(4):
            tracker.observe("exec", {"command": f"python t.py -{i}"}, _exec(stdout="same"))
        tracker.observe("exec", {"command": "python t.py -x"}, _exec(stdout="a wholly new result"))
        assert tracker.verdict("exec", {"command": "python t.py -y"}) is None

    def test_a_silent_command_is_noted_and_never_refused(self) -> None:
        """`mkdir -p`, `cp`, `rm -f` are byte-identical forever, whatever they did."""
        tracker = _tracker()
        for i in range(8):
            tracker.observe("exec", {"command": f"mkdir -p /w/out/{i}"}, _exec(stdout=""))
        verdict = tracker.verdict("exec", {"command": "mkdir -p /w/out/9"})
        assert verdict is None or verdict.action != "refused"

    def test_a_search_is_never_refused_only_noted(self) -> None:
        """Refusing a search buys one call and costs a capability."""
        tracker = _tracker()
        for i in range(8):
            tracker.observe(
                "search_files",
                {"path": "/w", "pattern": f"class_embed|pred_logits|presence|attempt{i}"},
                {"matches": [], "count": 0},
            )
        verdict = tracker.verdict(
            "search_files", {"path": "/w", "pattern": "class_embed|pred_logits|presence"}
        )
        assert verdict is None or verdict.action == "noted"


class TestTheFalsePositivesHostileReviewFound:
    """Two shapes that a Code task does on purpose, refused on the fifth call.

    2026-09-17. The signature kept only the first two non-flag tokens, and
    `-m` is a flag — so every `python -m pytest <file>` in a run was one
    family, and five different test files each printing the identical
    "1 passed" got the fifth refused with "varying the arguments is not
    varying the approach". `python solve.py --case N` did the same over six
    genuinely different cases. WildClaw Code tasks 2/7/8/12 are shaped as
    "run the solver over N cases", which is the workload being re-measured.
    """

    def _drive(self, commands: list[str], stdout: str) -> dict[str, list[int]]:
        tracker = _tracker()
        seen: dict[str, list[int]] = {"noted": [], "refused": []}
        for i, command in enumerate(commands):
            verdict = tracker.verdict("exec", {"command": command})
            if verdict:
                seen[verdict.action].append(i)
            tracker.observe("exec", {"command": command}, _exec(stdout=stdout))
        return seen

    def test_five_different_test_files_are_five_questions(self) -> None:
        commands = [
            f"python -m pytest tests/test_{name}.py -q"
            for name in ("alpha", "beta", "gamma", "delta", "epsilon")
        ]
        assert self._drive(commands, ".\n1 passed in 0.01s\n") == {"noted": [], "refused": []}

    def test_six_cases_of_one_solver_are_six_questions(self) -> None:
        commands = [f"python solve.py --case {i}" for i in range(6)]
        assert self._drive(commands, "OK\n") == {"noted": [], "refused": []}

    def test_the_same_target_with_only_flags_varying_is_still_one_question(self) -> None:
        """The narrowing must not disarm the guard on the shape it is for."""
        commands = [
            f"python test_sam3.py --tb={style}"
            for style in ("short", "long", "auto", "no", "line", "native")
        ]
        seen = self._drive(commands, "AssertionError: shapes do not match")
        assert seen["noted"] and seen["refused"]


class TestTheRecordedShapes:
    def test_the_thirteen_reads_of_one_module(self) -> None:
        """Eight windows, four of them repeats, two differing only by type.

        Verbatim from `sam3_debug`'s transcript. The exact guard sees thirteen
        distinct calls because `'130'` is not `130`.
        """
        path = "/tmp_workspace/sam3/sam3/model/sam3_image.py"
        windows = [
            {},
            {"limit": 90, "offset": 439},
            {"limit": 60, "offset": 519},
            {"limit": 130, "offset": 240},
            {"limit": 60, "offset": 315},
            {"limit": 75, "offset": 299},
            {"limit": "130", "offset": "250"},
            {"limit": 100, "offset": 165},
            {"limit": "55", "offset": "165"},
            {"limit": 200, "offset": 165},
            {"limit": "130", "offset": "240"},
            {"limit": "130", "offset": "250"},
            {"limit": "20", "offset": "325"},
        ]
        tracker = _tracker()
        bodies = {
            (240, 130): "def forward_grounding(self, x):",
            (250, 130): "def forward_grounding(self, x):",
            (165, 55): "class Sam3Image(nn.Module):",
        }
        escalated = False
        for window in windows:
            args = {"path": path, **window}
            if tracker.verdict("read_file", args) is not None:
                escalated = True
            key = (int(window.get("offset", 0)), int(window.get("limit", 0)))
            tracker.observe("read_file", args, {"content": bodies.get(key, f"chunk {key}")})
        assert escalated, "thirteen reads of one module, and nothing was ever said"

    def test_the_same_test_command_after_each_pip_install(self) -> None:
        commands = [
            "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -40",
            "cd /tmp_workspace && pip install --quiet einops 2>&1 | tail -5",
            "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -40",
            "cd /tmp_workspace && pip install --quiet triton 2>&1 | tail -10",
            "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -40",
            "cd /tmp_workspace && pip install --quiet pycocotools scipy 2>&1 | tail -5",
            "cd /tmp_workspace && python test_sam3.py 2>&1 | tail -40",
        ]
        tracker = _tracker()
        actions = []
        for command in commands:
            verdict = tracker.verdict("exec", {"command": command})
            if verdict:
                actions.append(verdict.action)
            # every run of the test prints the same assertion; the installs
            # each say something new.
            out = (
                "AssertionError: shapes do not match"
                if "test_sam3" in command
                else f"Successfully installed {command[-20:]}"
            )
            tracker.observe("exec", {"command": command}, _exec(stdout=out))
        assert "noted" in actions
