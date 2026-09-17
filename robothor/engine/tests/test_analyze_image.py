"""`analyze_image` — one question, many pictures, none of them in context.

Measured 2026-09-16 (reports/P4-HERMES-analysis.md, difference #4). On the
WildClawBench Productivity task that hands an agent a folder of photographs
and asks it to categorise them, the competing harness called its own vision
tool ONE HUNDRED times and scored 0.992. This engine called `view_image` four
times and scored 0.424 — classifying at 0.28 accuracy where five classes make
0.20 the score for guessing, which is what it was doing: guessing from
filenames.

The gap is not the model. `view_image` puts the picture into the PRIMARY
model's context: every call costs a turn, the image tokens stay in the
conversation for the rest of the run, and after three or four the agent stops
looking because looking has become expensive. The competing tool is out of
band — a separate vision call per image, only the text answer comes back —
so it is cheap enough to loop.

So this tool answers a QUESTION about a LIST of images, runs the vision calls
concurrently off to one side, and returns text. What is pinned here:

* the answers come back in the order the paths were given;
* the concurrency cap is real (measured as max observed in flight);
* one slow image fails alone — its neighbours still answer;
* a refused path (a secrets file, or anything resolving outside the
  workspace) never reaches a backend at all;
* the cap on how many images one call may take;
* NO image blocks in the result, ever — that is the whole point, and
  `session.py` keys its block-emitting convention on exactly those fields;
* a model that cannot accept images is never handed one.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from robothor.engine import vision_batch, vision_fallback

# ── fixtures ────────────────────────────────────────────────────────────────


def _png(path, size=(24, 18), color=(10, 120, 200)):
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return path


def _images(tmp_path, count=3):
    return [str(_png(tmp_path / f"shot{i}.png")) for i in range(count)]


class FakeVision:
    """A vision backend that answers, counts, and never touches a network.

    Records the maximum number of concurrent calls it ever saw, which is the
    only honest way to test a concurrency cap: asserting the semaphore's size
    tests the constructor, not the behaviour.
    """

    def __init__(self, answer="a cat", delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls: list[bytes] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, data: bytes, prompt: str = "", **kwargs: Any) -> str:
        self.calls.append(data)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return f"{self.answer} #{len(self.calls)}"
        finally:
            self.in_flight -= 1


@pytest.fixture
def local_backend(monkeypatch):
    """The local VLM is the configured backend, and it is a fake."""
    fake = FakeVision()
    monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
    monkeypatch.setattr(
        vision_batch,
        "resolve_backend",
        lambda: (vision_batch.Backend("local", "test-vlm"), ""),
    )
    return fake


async def _analyze(tmp_path, paths, question="what is this?", **kwargs):
    from robothor.engine.tools.dispatch import _collect_handlers

    ctx = type("Ctx", (), {"workspace": str(tmp_path), "run_id": "r1", "agent_id": "probe"})()
    args = {"paths": paths, "question": question, **kwargs}
    return await _collect_handlers()["analyze_image"](args, ctx)


# ── the shape of an answer ──────────────────────────────────────────────────


class TestManyImagesOneCall:
    async def test_every_image_gets_an_answer_in_order(self, tmp_path, local_backend):
        paths = _images(tmp_path, 5)
        out = await _analyze(tmp_path, paths)

        assert [r["path"] for r in out["results"]] == paths
        assert out["analyzed"] == 5
        assert out["failed"] == 0
        assert all(r["answer"] for r in out["results"])
        assert all(r["model"] == "test-vlm" for r in out["results"])
        assert all(isinstance(r["ms"], int) for r in out["results"])

    async def test_the_summary_says_what_happened(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 2))
        assert "2" in out["summary"]
        assert "test-vlm" in out["summary"]

    async def test_no_image_block_ever_reaches_the_agents_context(self, tmp_path, local_backend):
        """`session.py` emits a real image content block whenever a tool result
        carries `image_base64`/`screenshot_base64`. This tool must never trip
        that convention — an out-of-band tool that quietly refilled the context
        with pictures would be `view_image` with extra steps."""
        out = await _analyze(tmp_path, _images(tmp_path, 3))
        flat = repr(out)
        assert "image_base64" not in flat
        assert "screenshot_base64" not in flat
        assert "image_mime" not in flat
        assert "image_url" not in flat

    async def test_the_question_is_what_the_backend_is_asked(self, tmp_path, monkeypatch):
        seen: list[str] = []

        async def fake(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            seen.append(prompt)
            return "yes"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        await _analyze(tmp_path, _images(tmp_path, 2), question="is there a dog?")
        # The reply contract rides behind the question, never in front of it:
        # the agent's own words are the first thing the model reads.
        assert all(prompt.startswith("is there a dog?") for prompt in seen)
        assert len(seen) == 2


class TestConcurrency:
    async def test_the_cap_is_honoured(self, tmp_path, monkeypatch):
        fake = FakeVision(delay=0.02)
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 8), max_concurrency=2)
        assert out["analyzed"] == 8
        assert fake.max_in_flight <= 2, f"{fake.max_in_flight} calls were in flight at once"
        assert fake.max_in_flight == 2, "the cap should be reached, or nothing ran concurrently"

    async def test_an_absurd_request_is_clamped_not_obeyed(self, tmp_path, monkeypatch):
        fake = FakeVision(delay=0.02)
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        await _analyze(tmp_path, _images(tmp_path, 12), max_concurrency=500)
        assert fake.max_in_flight <= vision_batch.MAX_CONCURRENCY

    @pytest.mark.parametrize("requested", [12, 500])
    async def test_the_setting_is_a_ceiling_the_agent_cannot_raise(
        self, tmp_path, monkeypatch, requested
    ):
        """Hostile review I-3. With the setting at 4, an agent asking for 12
        got 12 and one asking for 500 got 16 — the operator's number was a
        default, not a limit. On a single-GPU box that is the self-inflicted
        queueing the number exists to prevent."""
        fake = FakeVision(delay=0.02)
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(vision_batch, "_configured_concurrency", lambda: 4)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        await _analyze(tmp_path, _images(tmp_path, 20), max_concurrency=requested)
        assert fake.max_in_flight <= 4, f"the operator said 4, {fake.max_in_flight} ran"

    async def test_an_agent_may_still_ask_to_go_gentler(self, tmp_path, monkeypatch):
        fake = FakeVision(delay=0.02)
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(vision_batch, "_configured_concurrency", lambda: 8)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        await _analyze(tmp_path, _images(tmp_path, 12), max_concurrency=2)
        assert fake.max_in_flight == 2

    def test_an_operator_cannot_exceed_the_platform_ceiling_either(self, monkeypatch):
        monkeypatch.setattr(vision_batch, "_configured_concurrency", lambda: 10_000)
        assert vision_batch._clamp_concurrency(None) == vision_batch.MAX_CONCURRENCY


class TestOneImageFailsAlone:
    async def test_a_timeout_marks_that_image_only(self, tmp_path, monkeypatch):
        paths = _images(tmp_path, 3)

        slow_index = 1
        calls = {"n": 0}

        async def fake(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            n = calls["n"]
            calls["n"] += 1
            if n == slow_index:
                await asyncio.sleep(5)
            return "fine"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        monkeypatch.setattr(vision_batch, "_per_image_timeout", lambda: 0.05)

        out = await _analyze(tmp_path, paths, max_concurrency=1)
        assert out["failed"] == 1
        assert out["analyzed"] == 2
        assert "timed out" in out["results"][slow_index]["error"]
        assert "answer" not in out["results"][slow_index]
        assert out["results"][0]["answer"] == "fine"
        assert out["results"][2]["answer"] == "fine"

    async def test_a_backend_that_raises_does_not_take_the_batch_down(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        async def fake(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the model is down")
            return "fine"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 2), max_concurrency=1)
        assert out["failed"] == 1
        assert "RuntimeError" in out["results"][0]["error"]
        assert out["results"][1]["answer"] == "fine"


class TestTheWholeCallDeadline:
    """The one mechanism between a dead backend and a tool call held open for
    75 minutes (200 images, four at a time, a 90 s per-image timeout). It
    shipped with no test at all — and a control nothing exercises is a control
    that can be deleted without anybody noticing."""

    @staticmethod
    def _hung_backend(monkeypatch, sleep_for=5.0):
        async def hangs(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            await asyncio.sleep(sleep_for)
            return "never"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", hangs)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )

    async def test_a_hung_backend_returns_inside_the_deadline_with_partial_rows(
        self, tmp_path, monkeypatch
    ):
        self._hung_backend(monkeypatch)
        monkeypatch.setattr(vision_batch, "_per_image_timeout", lambda: 0.05)
        monkeypatch.setattr(vision_batch, "_batch_deadline", lambda: 0.3)

        started = time.monotonic()
        out = await _analyze(tmp_path, _images(tmp_path, 60), max_concurrency=4)
        elapsed = time.monotonic() - started

        # Stated bound: the 0.3 s deadline plus one round of 0.05 s timeouts =
        # 0.35 s. Measured 0.30-0.42 s. The assertion sits at ~2.5x the bound —
        # loose enough for a busy CI box, tight enough that a regression to the
        # two seconds the old `< 3.0` allowed is caught. A control exercised
        # with an order of magnitude of slack is barely exercised.
        assert elapsed < 0.9, f"the deadline did not bound the call ({elapsed:.2f}s)"
        assert out["analyzed"] == 0
        assert out["failed"] == 60
        rows = (
            out["results"]
            if "results_file" not in out
            else json.loads(Path(out["results_file"]).read_text())["results"]
        )
        assert any("timed out" in r["error"] for r in rows), "the early images were tried"
        assert any("ran out of time" in r["error"] for r in rows), "the late ones were not"
        assert all("error" in r for r in rows)

    async def test_a_backend_that_ignores_cancellation_overshoots_by_one_round_only(
        self, tmp_path, monkeypatch
    ):
        """Measured by the reviewer: a 2.0 s deadline became 5.51 s. `wait_for`
        cancels the call and then waits for it, so a coroutine that swallows
        CancelledError sets the overshoot, not the timeout. Bounded by ONE
        in-flight round because nothing new starts past the deadline — which is
        the bound the module docstring states, pinned here."""

        async def stubborn(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                await asyncio.sleep(0.25)  # keeps going after being told to stop
                return "answered anyway"
            return "never"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", stubborn)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        monkeypatch.setattr(vision_batch, "_per_image_timeout", lambda: 0.05)
        monkeypatch.setattr(vision_batch, "_batch_deadline", lambda: 0.2)

        started = time.monotonic()
        out = await _analyze(tmp_path, _images(tmp_path, 40), max_concurrency=4)
        elapsed = time.monotonic() - started

        # Stated bound: the 0.2 s deadline plus one stubborn round of 0.25 s =
        # 0.45 s. Measured 0.31 s. Two rounds would be 0.7 s and fail.
        assert elapsed < 1.0, f"the overshoot was not bounded by one round ({elapsed:.2f}s)"
        assert len(out["results"]) or out.get("results_total")

    async def test_an_uncancellable_decode_overshoots_by_one_load_only(self, tmp_path, monkeypatch):
        """`asyncio.to_thread` cannot be cancelled: a thread decoding a
        7000x7000 PNG finishes whatever the deadline says. Measured by the
        reviewer at 2.48 s against a 2.0 s deadline. Bounded by one load."""

        def slow_load(path):
            time.sleep(0.3)
            return b"\x89PNG", "image/png"

        monkeypatch.setattr(vision_batch, "_load", slow_load)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )

        async def instant(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            return "fine"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", instant)
        monkeypatch.setattr(vision_batch, "_batch_deadline", lambda: 0.1)

        started = time.monotonic()
        await _analyze(tmp_path, _images(tmp_path, 20), max_concurrency=4)
        elapsed = time.monotonic() - started

        # Stated bound: the 0.1 s deadline plus one uncancellable 0.3 s load =
        # 0.4 s. Measured 0.30 s. A second round of loads would be 0.7 s.
        assert elapsed < 1.0, f"more than one round of loads ran past the deadline ({elapsed:.2f}s)"


class TestRefusedPaths:
    async def test_a_path_outside_the_workspace_never_reaches_the_backend(
        self, tmp_path, local_backend
    ):
        outside = tmp_path.parent / "outside.png"
        _png(outside)
        inside = _images(tmp_path, 1)
        out = await _analyze(tmp_path, [str(outside), *inside])

        assert "outside the workspace" in out["results"][0]["error"]
        assert out["results"][1]["answer"]
        assert len(local_backend.calls) == 1, "the refused image was still sent to a model"

    async def test_a_symlink_out_of_the_workspace_is_refused(self, tmp_path, local_backend):
        """The hostile probe: 199 honest paths and one symlink. Containment is
        judged on the RESOLVED path, so the link is followed first."""
        real = tmp_path.parent / "elsewhere.png"
        _png(real)
        link = tmp_path / "innocent.png"
        link.symlink_to(real)

        out = await _analyze(tmp_path, [str(link)])
        assert "outside the workspace" in out["results"][0]["error"]
        assert local_backend.calls == []

    async def test_a_secrets_file_is_refused_without_being_opened(self, tmp_path, local_backend):
        secret = tmp_path / ".env"
        secret.write_text("TOKEN=not-a-real-value\n")
        out = await _analyze(tmp_path, [str(secret)])
        assert "secrets file" in out["results"][0]["error"]
        assert local_backend.calls == []

    async def test_a_missing_file_is_reported_per_image(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, [str(tmp_path / "nope.png")])
        assert "no such file" in out["results"][0]["error"]
        assert local_backend.calls == []

    async def test_a_text_file_named_png_is_not_an_image(self, tmp_path, local_backend):
        fake_png = tmp_path / "notreally.png"
        fake_png.write_text("this is prose")
        out = await _analyze(tmp_path, [str(fake_png)])
        assert out["results"][0]["error"]
        assert local_backend.calls == []

    async def test_an_enormous_file_is_refused_before_it_is_decoded(
        self, tmp_path, local_backend, monkeypatch
    ):
        """The hostile probe's 50 MB image. Judged by `stat`, before Pillow is
        asked to decode it — a decompression bomb costs the same whether the
        result is used or thrown away."""
        monkeypatch.setattr(vision_batch, "MAX_IMAGE_BYTES", 1024)
        big = _png(tmp_path / "big.png", size=(400, 400))
        out = await _analyze(tmp_path, [str(big)])
        assert "too large" in out["results"][0]["error"]
        assert local_backend.calls == []


class TestTheAnswersStayBounded:
    async def test_a_narrating_model_is_cut_and_says_so(self, tmp_path, monkeypatch):
        """200 images times an unbounded answer is the context this tool exists
        to keep empty. Offloading only catches that for an agent whose manifest
        turns it on, so the cap does not depend on configuration."""

        async def verbose(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            return "a very long description. " * 500

        monkeypatch.setattr(vision_batch, "describe_image_bytes", verbose)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        answer = row["answer"]
        assert len(answer) <= vision_batch.MAX_ANSWER_CHARS + len(vision_batch._TRUNCATION_MARK)
        assert answer.endswith(vision_batch._TRUNCATION_MARK), "a cut answer must say it was cut"
        assert row["truncated"] is True, "a caller must not have to substring-match to find out"

    async def test_an_uncut_answer_carries_no_flag(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert "truncated" not in out["results"][0]


class TestSayingWhatWasIgnored:
    """Minors M-1 and M-6 of the hostile review: two knobs the agent may set
    that the tool quietly did not honour. An ignored request the caller is not
    told about is a caller that believes it looked closer."""

    async def test_detail_on_the_local_backend_says_it_was_ignored(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1), detail="high")
        assert "detail" in out["note"]
        assert "ignored" in out["note"]

    async def test_no_such_note_when_the_agent_asked_for_nothing(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert "note" not in out

    async def test_a_cut_question_says_so(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="q" * 5000)
        assert "cut" in out["note"]
        assert len(out["question"]) == vision_batch.MAX_QUESTION_CHARS


class TestOneRowShapeForEveryRow:
    async def test_a_refused_row_reports_the_same_path_form_as_an_answered_one(
        self, tmp_path, local_backend
    ):
        """M-3. An agent zipping its paths to the results by string got a
        mismatch on exactly the rows it needed to retry."""
        good = _images(tmp_path, 1)[0]
        sub = tmp_path / "nested"
        sub.mkdir()
        _png(sub / "missing_sibling.png")
        relative_missing = "nested/../nope.png"

        out = await _analyze(tmp_path, [good, relative_missing])
        answered, refused = out["results"]
        assert answered["path"] == good
        assert refused["path"] == str(tmp_path / "nope.png"), "resolved, like the answered row"


class TestArgumentGuards:
    async def test_more_than_the_cap_is_refused_as_a_whole(self, tmp_path, local_backend):
        one = _images(tmp_path, 1)[0]
        out = await _analyze(tmp_path, [one] * (vision_batch.MAX_PATHS + 1))
        assert "error" in out
        assert str(vision_batch.MAX_PATHS) in out["error"]
        assert local_backend.calls == []

    async def test_exactly_the_cap_is_allowed(self, tmp_path, local_backend):
        one = _images(tmp_path, 1)[0]
        out = await _analyze(tmp_path, [one] * vision_batch.MAX_PATHS)
        assert out["analyzed"] == vision_batch.MAX_PATHS

    async def test_no_paths_is_an_error(self, tmp_path, local_backend):
        assert "error" in await _analyze(tmp_path, [])

    async def test_a_missing_question_is_an_error(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="  ")
        assert "question" in out["error"]
        assert local_backend.calls == []


class TestBackendHonesty:
    def test_a_remote_model_that_cannot_see_is_not_dialled(self, monkeypatch):
        """The same `accepts_images` honesty `view_image` learned in #578: a
        model the registry says rejects images is never handed one."""
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "ollama_chat/qwen3:8b"
        )
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "")
        backend, refusal = vision_batch.resolve_backend()
        assert backend is None
        assert "accept images" in refusal

    def test_a_model_the_registry_has_never_heard_of_is_not_dialled(self, monkeypatch):
        """The hostile review's I-1, and the sharpest evidence for it: probing
        this hole sent two real calls to OpenRouter, which answered `404 No
        endpoints found that support image input` — the #578 failure, on the
        one setting whose whole job is to name a vision model.

        `!= "rejects"` waves through every model nobody has written an entry
        for. Declared able, or not dialled."""
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "")
        backend, refusal = vision_batch.resolve_backend()
        assert backend is None
        assert "openrouter/nobody/unheard-of-v9" in refusal
        assert "accepts_images" in refusal, "the refusal must name the field to set"

    def test_it_falls_back_to_the_local_model_rather_than_failing(self, monkeypatch):
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "ollama_chat/qwen3:8b"
        )
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "llava:7b")
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "local"
        assert backend.model == "llava:7b"
        assert refusal == ""
        assert "ollama_chat/qwen3:8b" in backend.note, "a silent fallback is a lie by omission"

    def test_an_unknown_model_falls_back_to_the_local_one_and_says_why(self, monkeypatch):
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "llava:7b")
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "local"
        assert refusal == ""
        assert "not declared" in backend.note

    async def test_the_fallback_note_reaches_the_agent(self, tmp_path, monkeypatch):
        fake = FakeVision()
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "llava:7b")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["backend"] == "local"
        assert "unheard-of-v9" in out["note"]

    def test_a_declared_vision_model_is_used_remotely(self, monkeypatch):
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "openrouter/z-ai/glm-5.3-flash"
        )
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "remote"
        assert backend.model == "openrouter/z-ai/glm-5.3-flash"
        assert refusal == ""

    async def test_no_vision_model_anywhere_says_so_plainly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision_fallback, "configured_remote_model", lambda: "")
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert "error" in out
        assert "vision model" in out["error"]
        assert "results" not in out


class TestTheRemoteBackend:
    """The OpenRouter path, with a fake client — no network, ever."""

    def _fake_response(self, text="a red square", prompt_tokens=800, completion_tokens=12):
        return type(
            "R",
            (),
            {
                "choices": [
                    type(
                        "C",
                        (),
                        {"message": type("M", (), {"content": text})()},
                    )()
                ],
                "usage": type(
                    "U",
                    (),
                    {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                )(),
            },
        )()

    async def test_the_image_rides_as_a_data_uri_and_the_answer_comes_back(
        self, tmp_path, monkeypatch
    ):
        seen: list[dict[str, Any]] = []

        async def fake_acompletion(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return self._fake_response()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="colour?")

        assert out["results"][0]["answer"] == "a red square"
        assert out["results"][0]["model"] == "openrouter/z-ai/glm-5.3-flash"
        content = seen[0]["messages"][-1]["content"]
        image_block = next(b for b in content if b["type"] == "image_url")
        assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
        assert any(
            str(b.get("text", "")).startswith("colour?") for b in content if b["type"] == "text"
        )

    async def test_tokens_and_cost_are_recorded_for_the_run(self, tmp_path, monkeypatch):
        """The runner adds a tool result's `cost_usd` to the run total, so an
        out-of-band call that reported nothing would spend money invisibly."""

        async def fake_acompletion(**kwargs: Any) -> Any:
            return self._fake_response()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 2))

        assert out["results"][0]["tokens"] == 812
        assert out["tokens"] == 1624
        assert out["cost_usd"] > 0
        assert out["cost_usd"] == pytest.approx(2 * (800 * 0.000_000_15 + 12 * 0.000_000_5))

    async def test_detail_is_passed_through_when_asked_for(self, tmp_path, monkeypatch):
        seen: list[dict[str, Any]] = []

        async def fake_acompletion(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return self._fake_response()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        await _analyze(tmp_path, _images(tmp_path, 1), detail="high")
        block = next(b for b in seen[0]["messages"][-1]["content"] if b["type"] == "image_url")
        assert block["image_url"]["detail"] == "high"


class TestTheWholeResultIsBounded:
    """Hostile review I-2. The per-answer cap bounds ONE answer; 200 of them
    serialise to ~40,000 characters of short yes/no rows (~10k tokens) and
    ~432,000 of long ones (~108k). The tool's own claim is "does not fill your
    context", and the session's offloading does not save it: the threshold is
    0 by default and the manifest this is measured on does not set it."""

    async def test_a_big_batch_returns_totals_and_a_file_not_the_table(
        self, tmp_path, local_backend, monkeypatch
    ):
        # 900 -> 1200. The result gained `provenance` and `provenance_note`
        # (~130 characters, fixed, whatever the batch size), and at 900 those
        # left no room for the preview rows this test is about — a budget so
        # tight that zero rows fit is a different regime from the one being
        # pinned here. The production default is 3500, clamped at 3800.
        budget = 1200
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: budget)
        out = await _analyze(tmp_path, _images(tmp_path, 40))

        assert len(json.dumps(out, default=str)) <= budget
        assert out["analyzed"] == 40, "the totals cover every image, not the preview"
        assert out["results_total"] == 40
        assert 0 < out["results_shown"] < 40

    async def test_a_budget_too_small_for_one_row_still_spills_and_still_bounds(
        self, tmp_path, local_backend, monkeypatch
    ):
        """Round-1 review M-4. The case above moved off 900 because provenance
        made a preview row stop fitting there — so this keeps 900 pinned
        rather than deleted. `_max_total_chars` clamps only the UPPER bound, so
        an operator may still set it this low; what must hold is that the
        result stays inside the budget, the table still reaches the file, and
        the totals still describe every image. A zero-row preview is a thin
        answer, never a lost one."""
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 40))

        assert len(json.dumps(out, default=str)) <= 900
        assert out["analyzed"] == 40
        assert out["results_total"] == 40
        assert out["results_shown"] == 0
        assert out["results_file"]
        assert len(out["results"]) == out["results_shown"]
        assert "results_file" in out["note"], "the note must say where the rest went"
        assert out["results_file"] not in out["note"], (
            "the path is already a field; repeating it in the note spends a long "
            "workspace path twice out of a budget measured in hundreds of characters"
        )

    async def test_the_file_holds_every_row_with_its_tokens_and_cost(self, tmp_path, monkeypatch):
        """The brief's 'cost/usage recorded per image on the run'. The step
        writer replaces any tool_output over 4000 chars with a flat head/tail
        string, so for a batch big enough to need this tool the per-image
        ledger survives ONLY here."""

        async def fake_acompletion(**kwargs: Any) -> Any:
            return TestTheRemoteBackend()._fake_response()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 30))

        spilled = json.loads(Path(out["results_file"]).read_text())
        assert len(spilled["results"]) == 30
        assert all(r["tokens"] == 812 for r in spilled["results"])
        assert all(r["cost_usd"] > 0 for r in spilled["results"])
        assert spilled["cost_usd"] == out["cost_usd"], "the totals must agree"

    async def test_the_file_lands_in_the_workspace_where_read_file_can_reach_it(
        self, tmp_path, local_backend, monkeypatch
    ):
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 30))
        written = Path(out["results_file"])
        assert written.is_file()
        assert tmp_path in written.parents
        assert written.parent.name == "analyze_image"
        from robothor.engine.secret_paths import is_secret_path

        assert not is_secret_path(written), "the agent must be allowed to read it back"

    async def test_two_batches_in_one_run_do_not_overwrite_each_other(
        self, tmp_path, local_backend, monkeypatch
    ):
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        first = await _analyze(tmp_path, _images(tmp_path, 30))
        second = await _analyze(tmp_path, _images(tmp_path, 30))
        assert first["results_file"] != second["results_file"]
        assert Path(first["results_file"]).is_file()

    async def test_a_small_batch_keeps_every_row_inline(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 3))
        assert len(out["results"]) == 3
        assert "results_file" not in out
        assert "results_shown" not in out

    async def test_an_unwritable_workspace_still_returns_a_bounded_result(
        self, tmp_path, local_backend, monkeypatch
    ):
        """Returning 108k tokens because the disk was full would end the run
        the budget exists to protect."""

        def no_disk(root, run_id):
            raise OSError("read-only file system")

        monkeypatch.setattr(vision_batch, "_spill_path", no_disk)
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 40))
        assert len(json.dumps(out, default=str)) <= 900
        assert "results_file" not in out
        assert "could not be written" in out["note"]
        assert out["analyzed"] == 40


class TestTheStepWriterCapInvariant:
    """Re-review R-1. The whole spill design rests on one claim: what the agent
    reads inline is also what the run record keeps. `tracking._truncate_json`
    replaces any `tool_output` over `MAX_TOOL_OUTPUT_CHARS` with a flat
    head/tail string, destroying the per-image ledger and the `results_file`
    pointer — silently.

    It shipped neither enforced nor tested, and survived at the default by 108
    characters: `_fit` probed with a 400-character placeholder note while the
    four real notes measure 1,025 together, so the difference was spent after
    the arithmetic was done. At a budget of 3,900 — which the setting's own
    text invites — the result came back 516 over.

    This is the direct analogue of
    `test_assistant_turn_recording.py::test_turn_cap_is_below_the_step_writer_cap`,
    which is the pin the round-1 report cited as the model and did not build.
    """

    @staticmethod
    def _every_note_firing(monkeypatch, tmp_path):
        """A workspace with a 195-character path, an undeclared remote model
        falling back to a local one, `detail` ignored, and an over-long
        question: all four notes, on the longest paths a result can carry."""
        deep = tmp_path
        while len(str(deep)) < 195:
            deep = deep / "wsdirectorysegment"
        deep.mkdir(parents=True, exist_ok=True)

        async def verbose(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            return "a long answer. " * 200

        monkeypatch.setattr(vision_batch, "describe_image_bytes", verbose)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(
            vision_fallback, "configured_local_model", lambda: "llama3.2-vision:11b"
        )
        return deep

    @pytest.mark.parametrize("budget", [vision_batch.DEFAULT_MAX_TOTAL_CHARS, 10_000])
    async def test_the_inline_result_never_exceeds_the_step_writer_cap(
        self, tmp_path, monkeypatch, budget
    ):
        from robothor.engine.tracking import MAX_TOOL_OUTPUT_CHARS, _truncate_json

        deep = self._every_note_firing(monkeypatch, tmp_path)
        monkeypatch.setattr(
            vision_batch, "_max_total_chars", lambda: min(budget, vision_batch.MAX_INLINE_CHARS)
        )
        out = await _analyze(
            deep, _images(deep, 200), question="q" * 5000, detail="high", max_concurrency=8
        )

        serialised = json.dumps(out, default=str)
        assert len(serialised) <= MAX_TOOL_OUTPUT_CHARS, (
            f"{len(serialised)} chars would be flattened by the step writer, "
            "taking the per-image ledger and the results_file pointer with it"
        )
        assert _truncate_json(out) is out, "the writer would have replaced this result"
        assert out["note"].count("ignored") >= 1, "the detail note must be in what was measured"
        assert "results_file" in out
        assert out["results_total"] == 200

    def test_the_budget_can_never_be_set_above_the_cap(self, monkeypatch):
        """The operator-facing half: the setting text says the default sits
        just under the writer's cap, which invites raising it."""
        from robothor.engine.tracking import MAX_TOOL_OUTPUT_CHARS

        assert vision_batch.MAX_INLINE_CHARS < MAX_TOOL_OUTPUT_CHARS
        for configured in (3900, 4000, 1_000_000):
            monkeypatch.setattr(
                vision_batch,
                "_settings",
                lambda c=configured: type(
                    "S", (), {"providers": type("P", (), {"vision_batch_max_chars": c})()}
                )(),
            )
            assert vision_batch._max_total_chars() == vision_batch.MAX_INLINE_CHARS

    @pytest.mark.parametrize("configured", [0, -1])
    def test_a_non_positive_budget_is_the_default_not_unbounded(self, monkeypatch, configured):
        """R-2: `if budget > 0` made 0 mean 'no bound at all' — 62,656
        characters inline, no spill file. An operator setting 0 to mean 'always
        spill' got the exact opposite, and nothing said so."""
        monkeypatch.setattr(
            vision_batch,
            "_settings",
            lambda: type(
                "S", (), {"providers": type("P", (), {"vision_batch_max_chars": configured})()}
            )(),
        )
        assert vision_batch._max_total_chars() == vision_batch.DEFAULT_MAX_TOTAL_CHARS

    async def test_a_zero_budget_still_spills(self, tmp_path, local_backend, monkeypatch):
        monkeypatch.setattr(
            vision_batch,
            "_settings",
            lambda: type("S", (), {"providers": type("P", (), {"vision_batch_max_chars": 0})()})(),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 60))
        assert len(json.dumps(out, default=str)) <= vision_batch.DEFAULT_MAX_TOTAL_CHARS
        assert out["results_file"]


class TestTheSpillDirectoryIsHousekept:
    """Re-review R-3. One ~67 KB JSON per big batch, forever, in a directory no
    operator looks at — and the file index was computed by globbing that
    directory, so every spill paid for its own growth."""

    def _spill_dir(self, tmp_path):
        return tmp_path / vision_batch.SPILL_DIRNAME

    async def test_old_tables_are_pruned_and_fresh_ones_are_not(
        self, tmp_path, local_backend, monkeypatch
    ):
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 30))
        stale = Path(out["results_file"])
        fresh = stale.with_name("other-run-1.json")
        fresh.write_text("{}")
        import os

        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))

        assert vision_batch.prune_spill_files(retention_days=7, workspace=tmp_path) == 1
        assert not stale.exists()
        assert fresh.exists(), "a table written today is still the run's own working file"

    def test_zero_days_disables_the_prune_rather_than_deleting_everything(self, tmp_path):
        directory = self._spill_dir(tmp_path)
        directory.mkdir(parents=True)
        (directory / "r-1.json").write_text("{}")
        assert vision_batch.prune_spill_files(retention_days=0, workspace=tmp_path) == 0
        assert (directory / "r-1.json").exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert vision_batch.prune_spill_files(retention_days=7, workspace=tmp_path) == 0

    def test_the_daily_sweep_actually_calls_it(self):
        """A pruner nothing invokes is a directory that still grows."""
        from pathlib import Path as _Path

        import robothor.engine.retention as retention

        source = _Path(retention.__file__).read_text(encoding="utf-8")
        assert "prune_spill_files" in source

    async def test_the_index_does_not_scan_the_directory(
        self, tmp_path, local_backend, monkeypatch
    ):
        """The glob assumed the directory is only ever appended to: delete
        `run-x-2.json` and the next spill recomputed index 3 and overwrote
        `run-x-3.json`. A run lives in one process, so the counter is the
        authority and the existence check covers the rest."""
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        vision_batch._SPILL_COUNTS.clear()
        first = Path((await _analyze(tmp_path, _images(tmp_path, 30)))["results_file"])
        second = Path((await _analyze(tmp_path, _images(tmp_path, 30)))["results_file"])
        first.unlink()
        third = Path((await _analyze(tmp_path, _images(tmp_path, 30)))["results_file"])

        assert {first.name, second.name, third.name} == {"r1-1.json", "r1-2.json", "r1-3.json"}
        assert second.exists() and third.exists(), "a spill overwrote an earlier table"


class TestTheResultIsOffloadableLikeAnyOther:
    def test_a_large_batch_result_offloads_instead_of_filling_the_context(self, tmp_path):
        """Nothing special is needed — the session already spills any oversized
        tool result to disk. What is pinned here is that this tool's result
        takes that path and does NOT take the image-block path."""
        from robothor.engine.models import TriggerType
        from robothor.engine.session import AgentSession

        session = AgentSession(
            agent_id="probe", trigger_type=TriggerType.MANUAL, tool_offload_threshold=2000
        )
        big = {
            "question": "what is it?",
            "results": [
                {"path": f"/ws/img{i}.png", "answer": "a long description " * 20, "ms": 12}
                for i in range(40)
            ],
        }
        session.record_tool_call(
            tool_name="analyze_image", tool_input={}, tool_output=big, tool_call_id="t1"
        )
        content = session.messages[-1]["content"]
        assert isinstance(content, str), "a batch of answers must never become content blocks"
        assert "[Full output:" in content


# ── choices, reasons, and believing your own eyes ───────────────────────────


class ContractVision:
    """A local VLM that answers in the reply contract, and counts its calls.

    Scripted per call rather than per image: the tests that need a second
    reply send ONE image, so call *n* gets reply *n* and everything after the
    last scripted reply repeats it.
    """

    def __init__(self, *replies: str):
        self.replies = list(replies) or ["ANSWER: yes\nWHY: a cat is asleep on the couch"]
        self.prompts: list[str] = []

    async def __call__(self, data: bytes, prompt: str = "", **kwargs: Any) -> str:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


@pytest.fixture
def scripted(monkeypatch):
    """Install a ContractVision as the local backend; return the installer."""

    def install(*replies: str) -> ContractVision:
        fake = ContractVision(*replies)
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        return fake

    return install


class TestTheAnswerIsOneOfTheChoices:
    """P4-TASK8. `analyze_image` answered 92.9% of a hundred-image
    categorisation correctly and the agent threw every answer away, because a
    bare `{"answer": "3"}` is unauditable against a confident-looking filename.
    `choices` makes the contract "a label from this set" rather than "a
    string", so a reply that is not a label is an error and never a label.
    """

    async def test_an_in_list_reply_becomes_a_validated_choice(self, tmp_path, scripted):
        scripted("ANSWER: chart\nWHY: a horizontal bar chart of aid flow from Sweden")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo", "text"])
        row = out["results"][0]
        assert row["choice"] == "chart"
        assert "answer" not in row, "a constrained row carries the validated choice, not both"
        assert out["analyzed"] == 1

    async def test_the_canonical_spelling_is_returned_not_the_models(self, tmp_path, scripted):
        """A row an agent groups by must not split on the model's punctuation."""
        scripted("ANSWER: **Chart**.\nWHY: bars and an axis")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert out["results"][0]["choice"] == "chart"

    async def test_a_bare_reply_with_no_marker_still_matches_a_choice(self, tmp_path, scripted):
        scripted("3")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["1", "2", "3", "4", "5"])
        assert out["results"][0]["choice"] == "3"

    async def test_an_off_list_reply_is_re_asked_exactly_once(self, tmp_path, scripted):
        fake = scripted(
            "ANSWER: a lovely bar chart\nWHY: bars",
            "ANSWER: chart\nWHY: bars and an axis",
        )
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert len(fake.prompts) == 2, "one re-ask, not none and not a retry loop"
        assert out["results"][0]["choice"] == "chart"

    async def test_a_twice_off_list_reply_is_an_error_not_a_label(self, tmp_path, scripted):
        fake = scripted("ANSWER: 图表\nWHY: 柱状图")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]
        assert len(fake.prompts) == 2, "exactly one re-ask, then the row is an error"
        assert "choice" not in row and "answer" not in row
        assert "not one of the choices" in row["error"]
        assert out["analyzed"] == 0
        assert out["failed"] == 1

    async def test_an_off_list_row_does_not_cost_the_batch(self, tmp_path, monkeypatch):
        class PerImage:
            """The first image answers off-list twice; the rest comply."""

            def __init__(self) -> None:
                self.seen = 0

            async def __call__(self, data: bytes, prompt: str = "", **kwargs: Any) -> str:
                self.seen += 1
                if self.seen <= 2:
                    return "ANSWER: banana\nWHY: no"
                return "ANSWER: photo\nWHY: a beach at sunset"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", PerImage())
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        out = await _analyze(
            tmp_path, _images(tmp_path, 3), choices=["photo", "chart"], max_concurrency=1
        )
        assert out["failed"] == 1
        assert out["analyzed"] == 2

    async def test_a_prefix_of_a_choice_is_never_coerced_to_it(self, tmp_path, scripted):
        """Hostile review: `choices` whose members are prefixes of each other.
        `cat` must not swallow `category`, and a reply of `categ` is neither."""
        fake = scripted("ANSWER: categ\nWHY: unclear")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["cat", "category"])
        assert "choice" not in out["results"][0]
        assert len(fake.prompts) == 2

    async def test_prefix_members_each_match_themselves(self, tmp_path, scripted):
        scripted("ANSWER: cat\nWHY: whiskers")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["cat", "category"])
        assert out["results"][0]["choice"] == "cat"

    async def test_the_choices_are_put_to_the_model(self, tmp_path, scripted):
        fake = scripted("ANSWER: photo\nWHY: a beach")
        await _analyze(tmp_path, _images(tmp_path, 1), choices=["photo", "chart"])
        assert "photo" in fake.prompts[0] and "chart" in fake.prompts[0]

    async def test_the_re_ask_says_what_was_wrong(self, tmp_path, scripted):
        fake = scripted("ANSWER: banana\nWHY: no", "ANSWER: photo\nWHY: a beach")
        await _analyze(tmp_path, _images(tmp_path, 1), choices=["photo", "chart"])
        assert "banana" in fake.prompts[1], "the correction must quote what was rejected"


class TestTheChoicesAreValidatedAtTheBoundary:
    @pytest.mark.parametrize("bad", [["only"], [], [str(n) for n in range(21)]])
    async def test_a_list_of_the_wrong_size_is_refused_whole(self, tmp_path, scripted, bad):
        fake = scripted()
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=bad)
        assert "error" in out
        assert not fake.prompts, "a refused call never reaches a backend"

    async def test_exactly_two_and_exactly_twenty_are_allowed(self, tmp_path, scripted):
        scripted("ANSWER: 0\nWHY: a zero")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=[str(n) for n in range(20)])
        assert out["results"][0]["choice"] == "0"

    @pytest.mark.parametrize("bad", ["notalist", 5, [1, 2], ["a", ""], ["a", "   "]])
    async def test_a_malformed_list_is_refused_whole(self, tmp_path, scripted, bad):
        fake = scripted()
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=bad)
        assert "error" in out
        assert not fake.prompts

    async def test_choices_that_collide_are_refused_rather_than_silently_merged(
        self, tmp_path, scripted
    ):
        """Two entries that normalise to the same label make "which one did it
        pick" unanswerable. Refuse, rather than pick one."""
        fake = scripted()
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["Chart", "chart."])
        assert "error" in out
        assert not fake.prompts

    async def test_an_over_long_choice_is_refused(self, tmp_path, scripted):
        out = await _analyze(
            tmp_path,
            _images(tmp_path, 1),
            choices=["a" * (vision_batch.MAX_CHOICE_CHARS + 1), "b"],
        )
        assert "error" in out

    async def test_a_choice_containing_the_truncation_mark_is_just_a_string(
        self, tmp_path, scripted
    ):
        """Hostile review: the mark must be a marker for a reader, never a
        signal the matcher reads."""
        label = f"odd{vision_batch._TRUNCATION_MARK}"
        scripted(f"ANSWER: {label}\nWHY: it says so")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=[label, "plain"])
        assert out["results"][0]["choice"] == label

    async def test_without_choices_nothing_changes(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 2))
        assert all("answer" in r for r in out["results"])
        assert all("choice" not in r for r in out["results"])


class TestEveryAnsweredRowCarriesItsReason:
    """The Hermes difference reduced to its active ingredient: `{"answer":"1"}`
    is dismissible, `{"choice":"1","reason":"horizontal bar chart of aid flow
    from Sweden"}` is not."""

    async def test_a_reason_comes_back_with_a_free_text_answer(self, tmp_path, scripted):
        scripted("ANSWER: a bar chart\nWHY: bars and a titled y axis")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        assert row["answer"] == "a bar chart"
        assert row["reason"] == "bars and a titled y axis"

    async def test_a_reason_comes_back_with_a_choice(self, tmp_path, scripted):
        scripted("ANSWER: chart\nWHY: bars and a titled y axis")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert out["results"][0]["reason"] == "bars and a titled y axis"

    async def test_a_long_reason_is_cut_and_says_so(self, tmp_path, scripted):
        scripted("ANSWER: chart\nWHY: " + "bars everywhere " * 60)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        reason = out["results"][0]["reason"]
        assert len(reason) <= vision_batch.MAX_REASON_CHARS + len(vision_batch._TRUNCATION_MARK)
        assert reason.endswith(vision_batch._TRUNCATION_MARK)

    async def test_an_error_row_carries_no_reason(self, tmp_path, scripted):
        scripted("ANSWER: banana\nWHY: a banana")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert "reason" not in out["results"][0]

    async def test_a_model_that_gives_no_reason_is_flagged_not_invented(
        self, tmp_path, local_backend
    ):
        """The local VLM fixture answers free text with no WHY line. The row
        must say the evidence is missing rather than quietly manufacture one
        out of the answer."""
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        assert "reason" not in row
        assert row["reason_missing"] is True

    async def test_a_missing_reason_does_not_cost_a_second_call(self, tmp_path, scripted):
        """A re-ask is for a WRONG answer. Paying twice for every image because
        a weak backend skips a marker would undo the cheap-enough-to-loop
        property the whole tool exists for."""
        fake = scripted("just a cat, no markers at all")
        await _analyze(tmp_path, _images(tmp_path, 1))
        assert len(fake.prompts) == 1

    async def test_the_reply_contract_is_asked_for(self, tmp_path, scripted):
        fake = scripted()
        await _analyze(tmp_path, _images(tmp_path, 1), question="what is this?")
        assert fake.prompts[0].startswith("what is this?")
        assert "WHY:" in fake.prompts[0]


class TestASampleSpansTheLabels:
    """Build item 3. "Spot-check before you trust it" is one free look rather
    than a `read_file` round — and a look at the first N rows of a sorted
    folder is a look at one label."""

    @staticmethod
    def _one_label_first(monkeypatch, count):
        """Every image but the last five answers `chart`."""
        served: list[str] = []

        async def by_position(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            served.append("x")
            label = "chart" if len(served) <= count - 5 else "photo"
            return f"ANSWER: {label}\nWHY: what the pixels show here"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", by_position)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )

    async def _spilled(self, tmp_path, monkeypatch, budget=1400, count=40):
        self._one_label_first(monkeypatch, count)
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: budget)
        return await _analyze(
            tmp_path, _images(tmp_path, count), choices=["chart", "photo"], max_concurrency=1
        )

    async def test_the_sample_is_not_the_first_rows(self, tmp_path, monkeypatch):
        out = await self._spilled(tmp_path, monkeypatch)
        labels = {row["choice"] for row in out["sample"]}
        assert labels == {"chart", "photo"}, "a sample of the first N would be all chart"

    async def test_the_sample_is_present_when_the_table_spills(self, tmp_path, monkeypatch):
        out = await self._spilled(tmp_path, monkeypatch)
        assert out["sample"], "the spot-check must not need a read_file round"
        assert all("reason" in row for row in out["sample"])
        assert len(json.dumps(out, default=str)) <= 1400

    async def test_the_spilled_file_still_holds_every_row(self, tmp_path, monkeypatch):
        out = await self._spilled(tmp_path, monkeypatch)
        spilled = json.loads(Path(out["results_file"]).read_text())
        assert len(spilled["results"]) == 40

    async def test_a_small_batch_needs_no_sample(self, tmp_path, scripted):
        scripted("ANSWER: chart\nWHY: bars")
        out = await _analyze(tmp_path, _images(tmp_path, 3), choices=["chart", "photo"])
        assert "sample" not in out, "every row is already inline; a sample would be a copy"


class TestThePixelsOutrankTheFilename:
    """The one sentence the agent needed and did not have. It wrote
    "the vision model is giving unreliable results" about answers that were
    92.9% correct, because the only other signal it had was a filename."""

    async def test_the_result_says_a_look_beats_a_name(self, tmp_path, local_backend):
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert "filename" in out["summary"]

    def test_the_schema_no_longer_asks_for_the_shortest_useful_answer(self):
        from robothor.engine.tools.schemas import get_engine_schemas

        schema = get_engine_schemas()["analyze_image"]["function"]
        flat = json.dumps(schema)
        assert "shortest useful answer" not in flat, (
            "that sentence is what produced the unauditable bare digit"
        )
        assert "choices" in schema["parameters"]["properties"]
        assert "reason" in flat
        assert "filename" in flat

    def test_the_description_still_fits_the_tool_search_cap(self):
        from robothor.engine.tools.registry import ToolRegistry
        from robothor.engine.tools.schemas import get_engine_schemas

        description = get_engine_schemas()["analyze_image"]["function"]["description"]
        assert len(description) <= ToolRegistry._SEARCH_DESC_MAX

    def test_the_instruction_contract_says_the_look_wins(self):
        from robothor.engine.prompts import BEHAVIORAL_RULES

        rule = BEHAVIORAL_RULES.split("18.")[1]
        assert "filename" in rule
        assert "view_image" in rule


class TestAnExtensionThatMissedIsNotADeadBatch:
    """P4-TASK8 §2.2. `retinal_scan_analysis.png` came back `no such file`;
    the real file is `.jpg`. `view_image` solved exactly this in
    `handlers/images.py::_same_stem_images` — and this module never imported
    it, so the same bug lived on in the sibling tool."""

    async def test_a_wrong_extension_resolves_to_the_one_file_that_shares_the_stem(
        self, tmp_path, local_backend
    ):
        _png(tmp_path / "retinal_scan.jpg")
        out = await _analyze(tmp_path, [str(tmp_path / "retinal_scan.png")])
        row = out["results"][0]
        assert "answer" in row, "the file is right there under another extension"
        assert row["path"] == str(tmp_path / "retinal_scan.jpg")
        assert row["resolved_from"] == str(tmp_path / "retinal_scan.png")

    async def test_two_candidates_are_a_question_for_the_agent_not_a_coin_flip(
        self, tmp_path, local_backend
    ):
        _png(tmp_path / "twin.jpg")
        _png(tmp_path / "twin.webp")
        out = await _analyze(tmp_path, [str(tmp_path / "twin.png")])
        error = out["results"][0]["error"]
        assert "twin.jpg" in error and "twin.webp" in error

    async def test_the_substitute_must_still_be_inside_the_workspace(self, tmp_path, local_backend):
        outside = tmp_path.parent / "outside_ws"
        outside.mkdir(exist_ok=True)
        _png(outside / "elsewhere.jpg")
        out = await _analyze(tmp_path, [str(outside / "elsewhere.png")])
        assert "outside the workspace" in out["results"][0]["error"]

    async def test_a_relative_miss_names_the_root_it_was_joined_to(self, tmp_path, local_backend):
        """Batch 1 of the measured run burned 50 paths on `no such file:
        3d_architectural_render.jpg` with no hint that a root join was tried."""
        out = await _analyze(tmp_path, ["3d_architectural_render.jpg"])
        error = out["results"][0]["error"]
        assert "no such file" in error
        assert str(tmp_path) in error, "say which root the relative path was joined to"


class TestTheInlineResultStillFitsWithReasons:
    async def test_two_hundred_choice_rows_with_capped_reasons_stay_inline_bounded(
        self, tmp_path, monkeypatch
    ):
        """The #581 invariant, re-measured with `choices` and `reason` present:
        the constrained choice frees the tokens the free-text answer spent, and
        the reason is capped, so the worst case must still land under the step
        writer's cap."""
        from robothor.engine.tracking import MAX_TOOL_OUTPUT_CHARS, _truncate_json

        async def maximal(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            return "ANSWER: category-five\nWHY: " + "densely worded evidence " * 40

        monkeypatch.setattr(vision_batch, "describe_image_bytes", maximal)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        deep = tmp_path
        while len(str(deep)) < 195:
            deep = deep / "wsdirectorysegment"
        deep.mkdir(parents=True, exist_ok=True)
        out = await _analyze(
            deep,
            _images(deep, 200),
            choices=[f"category-{n}" for n in ("one", "two", "three", "four", "five")],
            max_concurrency=8,
        )
        serialised = json.dumps(out, default=str)
        assert len(serialised) <= MAX_TOOL_OUTPUT_CHARS
        assert _truncate_json(out) is out
        assert out["results_total"] == 200


class TestTheMatcherIsEqualityNotResemblance:
    """`vision_contract` in isolation. The end-to-end tests above prove the
    tool behaves; these pin the one property everything else rests on, where a
    future edit will read it: a reply either IS one of the labels, normalised,
    or it is off-list. There is no third answer, and no distance function."""

    @staticmethod
    def _contract(*labels):
        from robothor.engine.vision_contract import build_contract

        contract, refusal = build_contract(list(labels))
        assert not refusal, refusal
        return contract

    @pytest.mark.parametrize(
        "reply",
        ["chart", "Chart", "  chart  ", "**chart**", "chart.", '"chart"', "CHART!", "`chart`"],
    )
    def test_typography_is_not_a_different_answer(self, reply):
        assert self._contract("chart", "photo").match(reply) == "chart"

    @pytest.mark.parametrize(
        "reply", ["char", "charts", "chart of sales", "a chart", "图表", "", "   ", "1"]
    )
    def test_everything_else_is_off_list(self, reply):
        assert self._contract("chart", "photo").match(reply) is None

    def test_the_caller_spelling_is_what_comes_back(self):
        assert self._contract("Charts & Tables", "Photos").match("charts & tables") == (
            "Charts & Tables"
        )

    def test_a_label_matches_itself_however_odd(self):
        """Including one built out of the characters the normaliser strips."""
        for odd in ("[draft]", "**bold**", "a.b.c", "1.", "yes!"):
            assert self._contract(odd, "other").match(odd) == odd

    def test_no_contract_matches_nothing(self):
        from robothor.engine.vision_contract import Contract

        assert Contract().match("anything") is None

    def test_a_reply_with_no_markers_is_the_whole_answer(self):
        from robothor.engine.vision_contract import Contract, parse_reply

        parsed = parse_reply("a cat on a sofa", Contract())
        assert parsed.answer == "a cat on a sofa"
        assert parsed.reason == ""
        assert parsed.rejected == ""

    def test_the_last_marked_line_wins(self):
        """A model that restates the instruction before obeying it puts the
        instruction first."""
        from robothor.engine.vision_contract import Contract, parse_reply

        parsed = parse_reply(
            "ANSWER: <your answer>\nWHY: <one sentence>\nANSWER: a cat\nWHY: whiskers",
            Contract(),
        )
        assert parsed.answer == "a cat"
        assert parsed.reason == "whiskers"

    def test_a_why_line_is_not_swallowed_into_an_unmarked_answer(self):
        from robothor.engine.vision_contract import Contract, parse_reply

        parsed = parse_reply("a cat on a sofa\nWHY: whiskers and a tail", Contract())
        assert parsed.answer == "a cat on a sofa"
        assert parsed.reason == "whiskers and a tail"


class TestTheParserMeetsTheModelWhereItWrites:
    """Hostile review I1. The matcher is tolerant and the PARSER was not, so
    three ordinary formatting habits each turned a whole batch into `error`
    rows at twice the cost — a strict regression on the exact workload this
    change exists to fix, because before it every one of those rows returned a
    usable `answer`.

    The re-ask cannot recover any of them: it restates the same contract to a
    model whose HABIT is the problem. These are parsing failures wearing a
    wrong answer's clothes, and the fix is parsing. Nothing here relaxes
    `Contract.match` — whatever is extracted still has to be a label, exactly.
    """

    #: Ten images per format, because the defect was a whole-batch one: the
    #: measured failure is "20 backend calls, analyzed 0, failed 10".
    BATCH = 10

    @pytest.mark.parametrize(
        ("label", "reply"),
        [
            ("both markers on one line", "ANSWER: chart WHY: it is a bar chart"),
            ("a JSON object", '{"answer": "chart", "why": "bars and an axis"}'),
            ("a JSON object under other keys", '{"choice": "chart", "reason": "bars"}'),
            ("a parenthetical gloss", "ANSWER: chart (a bar chart with a titled axis)"),
            ("a markdown fence", "```\nANSWER: chart\nWHY: bars and an axis\n```"),
            ("a fenced JSON object", '```json\n{"answer": "chart", "why": "bars"}\n```'),
            ("a <think> preamble", "<think>hmm, bars…</think>\nANSWER: chart\nWHY: bars"),
            ("lowercase markers", "answer: chart\nwhy: bars and an axis"),
            ("an inline REASON marker", "ANSWER: chart REASON: bars and an axis"),
        ],
    )
    async def test_a_whole_batch_is_answered_at_one_call_each(
        self, tmp_path, scripted, label, reply
    ):
        fake = scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, self.BATCH), choices=["photo", "chart"])
        assert out["analyzed"] == self.BATCH, f"{label}: {out['results'][0]}"
        assert out["failed"] == 0, label
        assert len(fake.prompts) == self.BATCH, f"{label}: a re-ask cannot fix a format habit"
        assert all(row["choice"] == "chart" for row in out["results"]), label

    @pytest.mark.parametrize(
        "reply",
        [
            "ANSWER: chart WHY: bars and an axis",
            '{"answer": "chart", "why": "bars and an axis"}',
            "ANSWER: chart (bars and an axis)",
            "```\nANSWER: chart\nWHY: bars and an axis\n```",
            "<think>hmm</think>\nANSWER: chart\nWHY: bars and an axis",
        ],
    )
    async def test_the_reason_survives_every_shape(self, tmp_path, scripted, reply):
        scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["photo", "chart"])
        assert out["results"][0]["reason"] == "bars and an axis"

    async def test_a_fence_does_not_become_part_of_a_free_text_answer(self, tmp_path, scripted):
        scripted("```\nANSWER: a bar chart\nWHY: bars and an axis\n```")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["results"][0]["answer"] == "a bar chart"
        assert out["results"][0]["reason"] == "bars and an axis"

    async def test_a_think_block_is_never_the_answer(self, tmp_path, scripted):
        scripted("<think>the filename says MRI but I see bars</think>\na bar chart")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["results"][0]["answer"] == "a bar chart"
        assert "think" not in out["results"][0]["answer"]

    async def test_a_lone_why_line_does_not_keep_its_marker(self, tmp_path, scripted):
        """M2. `WHY: a cat on a sofa` used to answer `WHY: a cat on a sofa` —
        the sentence twice, once with the marker still stuck to it."""
        scripted("WHY: a cat on a sofa")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        assert row["answer"] == "a cat on a sofa"
        assert "WHY" not in row["answer"]

    async def test_a_gloss_is_only_read_when_the_head_is_a_real_label(self, tmp_path, scripted):
        """The parenthetical split is a last resort, not a licence to trim
        until something matches. `banana (a chart)` names no choice."""
        fake = scripted("ANSWER: banana (a chart)")
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["photo", "chart"])
        assert "choice" not in out["results"][0]
        assert "not one of the choices" in out["results"][0]["error"]
        assert len(fake.prompts) == 2

    async def test_free_text_keeps_its_parentheses(self, tmp_path, scripted):
        """Without `choices` there is no way to tell a gloss from the answer,
        so nothing is split off and nothing is lost."""
        scripted("ANSWER: a red square (with a black border)\nWHY: I can see it")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["results"][0]["answer"] == "a red square (with a black border)"

    async def test_a_json_reply_that_names_no_choice_is_still_off_list(self, tmp_path, scripted):
        fake = scripted('{"answer": "banana", "why": "a banana"}')
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["photo", "chart"])
        assert "choice" not in out["results"][0]
        assert len(fake.prompts) == 2, "one re-ask, as for any other off-list answer"

    async def test_a_json_array_is_not_an_object_and_is_read_as_text(self, tmp_path, scripted):
        """Only a JSON OBJECT carries named keys. A list is just a reply."""
        scripted('["chart"]')
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["results"][0]["answer"] == '["chart"]'

    def test_the_correction_shows_the_shape_once_with_a_real_label(self):
        from robothor.engine.vision_contract import build_contract, correction_suffix

        contract, _ = build_contract(["photo", "chart"])
        correction = correction_suffix(contract, "banana")
        assert correction.count("ANSWER:") == 1, "one worked example, not two contracts"
        assert correction.count("WHY:") == 1
        assert "ANSWER: photo" in correction, "show a real label, not a placeholder"
        assert "banana" in correction, "quote what was rejected"
        assert "Reply in exactly two lines" not in correction, (
            "restating the contract a model has already ignored is the least likely "
            "thing to change its mind; showing the exact bytes is the most"
        )


class TestTheSampleNeverPadsItself:
    """Hostile review I3. After collecting one row per distinct answer the
    sample topped itself up to five with ANY answered row — i.e. more copies of
    a label already in it. Measured: a 60-row two-label sort spent 3 of its 5
    sample rows on duplicates and returned 6 rows under `results`; a 200-row
    one-label batch spent 4 and returned 4.

    Those duplicates come straight out of the `results` allowance, because the
    sample is measured inside the same `_fit` probe. `_spill`'s own docstring
    says that is the wrong trade — "a spot-check is worth more than the fourth
    copy of one label, and nothing at all under `results` is worth less than
    either" — and both it and `docs/TOOLS.md` tell the reader the sample SPANS
    the distinct answers, which after the top-up it did not.
    """

    @staticmethod
    def _labelled(monkeypatch, labels):
        """A backend that answers `labels[i]` for the i-th image, in order."""
        served: list[str] = []

        async def by_position(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            served.append("x")
            label = labels[(len(served) - 1) % len(labels)]
            return f"ANSWER: {label}\nWHY: what the pixels show in this one"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", by_position)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )

    async def _spilled(self, tmp_path, monkeypatch, labels, count=60):
        """At the DEFAULT budget, which is where the review measured it and
        the only number an operator actually runs.

        Always offers at least two choices — one is refused at the boundary,
        and a batch that USES only one of the labels on offer is the case that
        matters here anyway.
        """
        self._labelled(monkeypatch, labels)
        offered = sorted(set(labels) | {"a-label-the-backend-never-picks"})
        return await _analyze(
            tmp_path, _images(tmp_path, count), choices=offered, max_concurrency=1
        )

    @pytest.mark.parametrize("labels", [["chart"], ["chart", "photo"], ["a", "b", "c"]])
    async def test_the_sample_is_exactly_the_distinct_answers(self, tmp_path, monkeypatch, labels):
        out = await self._spilled(tmp_path, monkeypatch, labels)
        seen = [row["choice"] for row in out["sample"]]
        assert sorted(seen) == sorted(set(labels)), "one row per answer, and no more"
        assert len(seen) == len(set(seen)), "a duplicate label is a wasted spot-check"

    async def test_more_answers_than_the_cap_are_still_capped(self, tmp_path, monkeypatch):
        labels = [f"label-{n}" for n in range(8)]
        out = await self._spilled(tmp_path, monkeypatch, labels)
        assert len(out["sample"]) == vision_batch.MAX_SAMPLE_ROWS
        seen = [row["choice"] for row in out["sample"]]
        assert len(seen) == len(set(seen))

    async def test_dropping_the_padding_hands_rows_back_to_results(self, tmp_path, monkeypatch):
        """The whole point: the characters the duplicates were spending are
        `results` rows, which is what the agent actually reads."""
        out = await self._spilled(tmp_path, monkeypatch, ["chart", "photo"])
        assert len(out["sample"]) == 2
        assert out["results_shown"] >= 8, (
            f"only {out['results_shown']} rows inline — the sample is still eating them"
        )

    async def test_a_free_text_batch_samples_distinct_answers_too(self, tmp_path, monkeypatch):
        """Without `choices` every answer tends to differ, so the cap is what
        bounds it — but two identical answers must still not both appear."""
        self._labelled(monkeypatch, ["a cat", "a cat", "a dog"])
        out = await _analyze(tmp_path, _images(tmp_path, 60), max_concurrency=1)
        seen = [row["answer"] for row in out["sample"]]
        assert len(seen) == len(set(seen)) == 2


class TestARowNeverLosesWhatItAlreadyPaidFor:
    """Hostile review M5. The re-ask shares the first call's per-image budget —
    both calls sit inside one `asyncio.wait_for` — which is the right bound
    (the module's "deadline plus ONE round" guarantee depends on it) but used
    to lose the first call's ledger: a slow first reply near the deadline
    turned an off-list row into a plain `timed out` row with no `reasked` and
    no `tokens`/`cost_usd`, so a remote backend's spend went unreported
    exactly when a batch was running long.

    The money was spent. The row says so.
    """

    @staticmethod
    def _remote(monkeypatch, replies, delay=0.0):
        seen: list[int] = []

        async def fake_acompletion(**kwargs: Any) -> Any:
            seen.append(1)
            if delay:
                await asyncio.sleep(delay)
            text = replies[min(len(seen) - 1, len(replies) - 1)]
            return type(
                "R",
                (),
                {
                    "choices": [type("C", (), {"message": type("M", (), {"content": text})()})()],
                    "usage": type("U", (), {"prompt_tokens": 800, "completion_tokens": 12})(),
                },
            )()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        return seen

    async def test_a_timeout_mid_re_ask_still_reports_the_first_calls_spend(
        self, tmp_path, monkeypatch
    ):
        self._remote(monkeypatch, ["ANSWER: banana\nWHY: no"], delay=0.20)
        monkeypatch.setattr(vision_batch, "_per_image_timeout", lambda: 0.30)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]

        assert "timed out" in row["error"]
        assert row["reasked"] is True, "the agent must see that a second call was started"
        assert row["tokens"] == 812, "the first call was paid for"
        assert row["cost_usd"] > 0
        assert out["tokens"] == 812, "and the batch total must agree"
        assert out["cost_usd"] > 0

    async def test_a_re_ask_that_raises_also_keeps_the_first_calls_spend(
        self, tmp_path, monkeypatch
    ):
        seen: list[int] = []

        async def fake_acompletion(**kwargs: Any) -> Any:
            seen.append(1)
            if len(seen) > 1:
                raise RuntimeError("provider went away")
            return type(
                "R",
                (),
                {
                    "choices": [
                        type(
                            "C",
                            (),
                            {"message": type("M", (), {"content": "ANSWER: banana"})()},
                        )()
                    ],
                    "usage": type("U", (), {"prompt_tokens": 800, "completion_tokens": 12})(),
                },
            )()

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_acompletion)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("remote", "openrouter/z-ai/glm-5.3-flash"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]
        assert "provider went away" in row["error"]
        assert row["tokens"] == 812
        assert row["reasked"] is True

    async def test_a_row_that_never_re_asked_claims_nothing(self, tmp_path, monkeypatch):
        self._remote(monkeypatch, ["ANSWER: chart\nWHY: bars"], delay=0.20)
        monkeypatch.setattr(vision_batch, "_per_image_timeout", lambda: 0.05)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]
        assert "timed out" in row["error"]
        assert "reasked" not in row, "nothing was re-asked, so nothing is claimed"
        assert "tokens" not in row


class TestTheSpillNoteDescribesWhatIsActuallyThere:
    """Hostile review M4. The note rendered "so **the first 1 are here**" in
    the common heavily-spilled case, said "none of them fit here" right after
    "did not fit", and promised the file holds "**answer**, reason, tokens and
    cost" when a `choices` batch's rows carry `choice`. A note is the one part
    of the result an agent reads as prose; it has to describe the result it is
    attached to."""

    @staticmethod
    def _sentence(total, shown, sampled=True, field="answer"):
        return vision_batch._spill_sentence(
            total, shown, Path("/ws/.robothor/analyze_image/r-1.json"), sampled, field
        )

    def test_one_row_is_singular(self):
        assert "the first row is here" in self._sentence(200, 1)
        assert "the first 1 are" not in self._sentence(200, 1)

    def test_several_rows_are_plural(self):
        assert "the first 7 are here" in self._sentence(200, 7)

    def test_no_rows_does_not_repeat_did_not_fit(self):
        sentence = self._sentence(200, 0)
        assert sentence.count("did not fit") == 1
        assert "none of them fit here" not in sentence

    def test_the_file_is_described_by_the_key_the_rows_actually_use(self):
        assert "choice, reason" in self._sentence(200, 7, field="choice")
        assert "answer, reason" in self._sentence(200, 7, field="answer")

    def test_the_sample_is_only_promised_when_there_is_one(self):
        assert "sample" in self._sentence(200, 7, sampled=True)
        assert "sample" not in self._sentence(200, 7, sampled=False)

    async def test_a_choices_batch_says_choice_in_its_note(self, tmp_path, monkeypatch):
        async def answer(data: bytes, prompt: str = "", **kwargs: Any) -> str:
            return "ANSWER: chart\nWHY: " + "bars and a titled axis, at length " * 3

        monkeypatch.setattr(vision_batch, "describe_image_bytes", answer)
        monkeypatch.setattr(
            vision_batch,
            "resolve_backend",
            lambda: (vision_batch.Backend("local", "test-vlm"), ""),
        )
        out = await _analyze(tmp_path, _images(tmp_path, 60), choices=["chart", "photo"])
        assert "choice, reason" in out["note"], out["note"]
        assert "answer, reason" not in out["note"]


class TestAFreeTextReplyIsNeverSilentlyTruncated:
    """Re-check O1. The I1 parser ran `_from_json` before it knew whether a
    choice was even required, so on a FREE-TEXT question a transcribed JSON
    payload that happened to carry an `answer` key collapsed to that one
    value — three fields gone, and the row said nothing about it.

    Two near-identical screenshots must not transcribe differently depending
    on whether the payload happens to use one of the parser's key names. So
    the JSON reader is gated on SHAPE: with no `choices`, its result is taken
    only when the object looks like a reply to the two-line contract — an
    answer key AND a reason key — and otherwise the whole reply comes back as
    text. The `choices` path is untouched: there, an extracted string still
    has to be a label, so there was never any exposure.
    """

    @pytest.mark.parametrize(
        "reply",
        [
            '{"question_id": 7, "answer": "42", "asked_by": "kiosk"}',
            '{"answer": "42"}',
            '{"total": 19.5, "vendor": "kiosk"}',
            '{"category": "produce", "sku": "A-1", "qty": 3}',
            '{"label": "urgent", "ticket": 88}',
        ],
    )
    async def test_a_transcribed_json_payload_comes_back_whole(self, tmp_path, scripted, reply):
        scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="transcribe the JSON shown")
        assert out["results"][0]["answer"] == reply, "every field the image showed"

    @pytest.mark.parametrize(
        ("reply", "answer", "reason"),
        [
            ('{"answer": "42", "why": "the big number"}', "42", "the big number"),
            ('{"choice": "42", "reason": "the big number"}', "42", "the big number"),
            ('{"label": "42", "explanation": "the big number"}', "42", "the big number"),
        ],
    )
    async def test_an_answer_plus_reason_object_is_still_read_as_a_reply(
        self, tmp_path, scripted, reply, answer, reason
    ):
        """This is a real reply to the contract and must keep working — it is
        why the gate is on shape rather than on the `choices` path."""
        scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="what number is shown?")
        row = out["results"][0]
        assert row["answer"] == answer
        assert row["reason"] == reason

    async def test_the_choices_path_still_reads_a_bare_answer_object(self, tmp_path, scripted):
        """No exposure there, so no gate there: the extracted string still has
        to be one of the labels, and a data object simply fails to be one."""
        scripted('{"answer": "chart"}')
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert out["results"][0]["choice"] == "chart"

    async def test_a_data_object_on_the_choices_path_is_off_list_not_a_label(
        self, tmp_path, scripted
    ):
        fake = scripted('{"question_id": 7, "answer": "42", "asked_by": "kiosk"}')
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        assert "choice" not in out["results"][0]
        assert len(fake.prompts) == 2

    async def test_a_json_transcription_keeps_its_reason_when_the_model_gives_one(
        self, tmp_path, scripted
    ):
        """Markers outside the object still work — the object is the answer."""
        scripted('{"total": 19.5}\nWHY: a receipt total in the corner')
        out = await _analyze(tmp_path, _images(tmp_path, 1), question="transcribe it")
        row = out["results"][0]
        assert row["answer"] == '{"total": 19.5}'
        assert row["reason"] == "a receipt total in the corner"


class TestMarkdownEmphasisNeverReachesTheRow:
    """Re-check F1. `_INLINE_WHY` strips markdown before the marker word and
    between it and the colon, but not the `**` that closes the bold AFTER the
    colon — so `**ANSWER:** chart **WHY:** bars` yielded `'** chart'` and
    `'** bars'`. The choice survived (the matcher strips emphasis on both
    sides) but the free-text answer and every reason carried the leftovers."""

    @pytest.mark.parametrize(
        "reply",
        [
            "**ANSWER:** chart **WHY:** bars and an axis",
            "**ANSWER:** chart\n**WHY:** bars and an axis",
            "*ANSWER:* chart *WHY:* bars and an axis",
            "__ANSWER:__ chart __WHY:__ bars and an axis",
        ],
    )
    async def test_bold_markers_leave_nothing_behind(self, tmp_path, scripted, reply):
        scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]
        assert row["choice"] == "chart"
        assert row["reason"] == "bars and an axis"

    async def test_a_free_text_answer_is_not_left_holding_the_asterisks(self, tmp_path, scripted):
        scripted("**ANSWER:** a bar chart **WHY:** bars and an axis")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        assert row["answer"] == "a bar chart"
        assert row["reason"] == "bars and an axis"


class TestBacktickedMarkersAreStillMarkers:
    """Re-check R2-1. The three marker patterns each carried a hand-written
    prefix class — `[\\s*_#>\\-]` — that listed asterisk and underscore but not
    the backtick, so a model that writes its markers as inline code got the
    whole reply rejected, re-asked, and turned into an `error` at twice the
    cost. Backtick is as ordinary a way to write `ANSWER:` as bold is.

    The same drift is why this now comes from ONE constant: three copies of a
    character class is three chances to forget the next character."""

    SHAPES = [
        "`ANSWER:` chart `WHY:` bars and an axis",
        "`ANSWER`: chart `WHY`: bars and an axis",
        "**`ANSWER:`** chart **`WHY:`** bars and an axis",
        "ANSWER: chart `WHY:` bars and an axis",
        "```\n`ANSWER:` chart\n`WHY:` bars and an axis\n```",
    ]

    @pytest.mark.parametrize("reply", SHAPES)
    async def test_the_choices_path_reads_them_in_one_call(self, tmp_path, scripted, reply):
        fake = scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1), choices=["chart", "photo"])
        row = out["results"][0]
        assert row["choice"] == "chart", row
        assert len(fake.prompts) == 1, "a marker style must never cost a re-ask"

    @pytest.mark.parametrize("reply", SHAPES)
    async def test_the_free_text_path_reads_them_too(self, tmp_path, scripted, reply):
        scripted(reply)
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        row = out["results"][0]
        assert row["answer"] == "chart", row
        assert row["reason"] == "bars and an axis", row

    async def test_a_backticked_batch_is_not_a_batch_of_errors(self, tmp_path, scripted):
        """The shape of the defect: it was never one row, it was all of them."""
        fake = scripted("`ANSWER:` chart `WHY:` bars and an axis")
        out = await _analyze(tmp_path, _images(tmp_path, 10), choices=["chart", "photo"])
        assert out["analyzed"] == 10
        assert out["failed"] == 0
        assert len(fake.prompts) == 10

    def test_one_constant_feeds_every_marker_pattern(self):
        """Three copies of a character class is three chances to forget the
        next character, which is exactly how the backtick went missing."""
        from robothor.engine import vision_contract as vc

        for pattern in (vc._ANSWER_LINE, vc._WHY_LINE, vc._INLINE_WHY):
            assert vc._MARKER_EDGE in pattern.pattern

    def test_a_marker_word_inside_a_sentence_is_still_not_a_marker(self):
        """The prefix class widening must not turn prose into a split."""
        from robothor.engine.vision_contract import Contract, parse_reply

        parsed = parse_reply("somewhy: not a marker", Contract())
        assert parsed.answer == "somewhy: not a marker"
        assert parsed.reason == ""
