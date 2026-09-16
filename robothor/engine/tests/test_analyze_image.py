"""`analyze_image` — one question, many pictures, none of them in context.

Measured 2026-09-16 (reports/P4-HERMES-analysis.md, difference #4). On the
WildClawBench Productivity task that hands an agent a folder of photographs
and asks it to categorise them, the competing harness called its own vision
tool ONE HUNDRED times and scored 0.99. This engine called `view_image` four
times and scored 0.28 — near random.

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

from robothor.engine import vision_batch

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
        assert seen == ["is there a dog?", "is there a dog?"]


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

        assert elapsed < 3.0, f"the deadline did not bound the call ({elapsed:.1f}s)"
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

        # One round of four images, each overshooting by 0.25 s, is the bound.
        assert elapsed < 3.0, f"the overshoot was not bounded by one round ({elapsed:.1f}s)"
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

        assert elapsed < 2.0, f"more than one round of loads ran past the deadline ({elapsed:.1f}s)"


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
        answer = out["results"][0]["answer"]
        assert len(answer) <= vision_batch.MAX_ANSWER_CHARS + len(vision_batch._TRUNCATION_MARK)
        assert answer.endswith(vision_batch._TRUNCATION_MARK), "a cut answer must say it was cut"


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
            vision_batch, "_configured_remote_model", lambda: "ollama_chat/qwen3:8b"
        )
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "")
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
            vision_batch, "_configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "")
        backend, refusal = vision_batch.resolve_backend()
        assert backend is None
        assert "openrouter/nobody/unheard-of-v9" in refusal
        assert "accepts_images" in refusal, "the refusal must name the field to set"

    def test_it_falls_back_to_the_local_model_rather_than_failing(self, monkeypatch):
        monkeypatch.setattr(
            vision_batch, "_configured_remote_model", lambda: "ollama_chat/qwen3:8b"
        )
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "llava:7b")
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "local"
        assert backend.model == "llava:7b"
        assert refusal == ""
        assert "ollama_chat/qwen3:8b" in backend.note, "a silent fallback is a lie by omission"

    def test_an_unknown_model_falls_back_to_the_local_one_and_says_why(self, monkeypatch):
        monkeypatch.setattr(
            vision_batch, "_configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "llava:7b")
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "local"
        assert refusal == ""
        assert "not declared" in backend.note

    async def test_the_fallback_note_reaches_the_agent(self, tmp_path, monkeypatch):
        fake = FakeVision()
        monkeypatch.setattr(vision_batch, "describe_image_bytes", fake)
        monkeypatch.setattr(
            vision_batch, "_configured_remote_model", lambda: "openrouter/nobody/unheard-of-v9"
        )
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "llava:7b")
        out = await _analyze(tmp_path, _images(tmp_path, 1))
        assert out["backend"] == "local"
        assert "unheard-of-v9" in out["note"]

    def test_a_declared_vision_model_is_used_remotely(self, monkeypatch):
        monkeypatch.setattr(
            vision_batch, "_configured_remote_model", lambda: "openrouter/z-ai/glm-5.3-flash"
        )
        backend, refusal = vision_batch.resolve_backend()
        assert backend.kind == "remote"
        assert backend.model == "openrouter/z-ai/glm-5.3-flash"
        assert refusal == ""

    async def test_no_vision_model_anywhere_says_so_plainly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision_batch, "_configured_remote_model", lambda: "")
        monkeypatch.setattr(vision_batch, "_configured_local_model", lambda: "")
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

        monkeypatch.setattr(vision_batch, "pooled_acompletion", fake_acompletion)
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
        assert any(b.get("text") == "colour?" for b in content if b["type"] == "text")

    async def test_tokens_and_cost_are_recorded_for_the_run(self, tmp_path, monkeypatch):
        """The runner adds a tool result's `cost_usd` to the run total, so an
        out-of-band call that reported nothing would spend money invisibly."""

        async def fake_acompletion(**kwargs: Any) -> Any:
            return self._fake_response()

        monkeypatch.setattr(vision_batch, "pooled_acompletion", fake_acompletion)
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

        monkeypatch.setattr(vision_batch, "pooled_acompletion", fake_acompletion)
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
        monkeypatch.setattr(vision_batch, "_max_total_chars", lambda: 900)
        out = await _analyze(tmp_path, _images(tmp_path, 40))

        assert len(json.dumps(out, default=str)) <= 900
        assert out["analyzed"] == 40, "the totals cover every image, not the preview"
        assert out["results_total"] == 40
        assert 0 < out["results_shown"] < 40
        assert len(out["results"]) == out["results_shown"]
        assert out["results_file"] in out["note"], "the note must say where the rest went"

    async def test_the_file_holds_every_row_with_its_tokens_and_cost(self, tmp_path, monkeypatch):
        """The brief's 'cost/usage recorded per image on the run'. The step
        writer replaces any tool_output over 4000 chars with a flat head/tail
        string, so for a batch big enough to need this tool the per-image
        ledger survives ONLY here."""

        async def fake_acompletion(**kwargs: Any) -> Any:
            return TestTheRemoteBackend()._fake_response()

        monkeypatch.setattr(vision_batch, "pooled_acompletion", fake_acompletion)
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
