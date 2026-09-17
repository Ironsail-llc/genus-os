"""Every image tool can see, and every answer says where it came from.

Measured 2026-09-17. A run in a container with no GPU asked `view_image` a
hundred times and was told, every time, that the primary model "cannot accept
images and the local vision model is unavailable (ConnectError)". The instance
had a remote vision model configured and `analyze_image` was using it happily
— `view_image` simply did not know about the second rung. So the agent could
not look for itself, and when the out-of-band batch disagreed with the
filenames it had no way to check which was right. It sided with the names and
scored zero on a task its own tool results were worth 0.94 on.

Two defects, one test file:

* **The ladder.** `view_image` climbs the same rungs `analyze_image` does —
  the primary model if it accepts images, then the local VLM, then the
  configured remote vision model — and only says "nobody looked" when every
  rung is genuinely out. A rung that failed is named, so a misconfiguration
  reads as a misconfiguration rather than as an absent capability.
* **Provenance.** Neither tool said where its answer came from. A model
  reading `answer: "natural scene"` for a file called
  `3d_architectural_render.jpg` has no cue that the answer was produced from
  the pixels and the name was never consulted. Now both say so, in the result
  and in the tool description, alongside the general rule that an observation
  outranks a name.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.engine import model_registry as mr

#: A model this repo's registry declares `accepts_images=True`. Used instead of
#: a monkeypatched capability so the test exercises the real gate: a remote
#: model the registry has never heard of must NOT be dialled.
DECLARED_VISION_MODEL = "openrouter/z-ai/glm-5.3-flash"

#: A text-only primary, so every test here starts on the fallback ladder.
TEXT_ONLY_PRIMARY = "ollama_chat/qwen3:8b"


@pytest.fixture(autouse=True)
def _clean_capability_state():
    mr.reset_image_discoveries()
    token = mr.note_active_model("")
    yield
    mr.reset_active_model(token)
    mr.reset_image_discoveries()


@pytest.fixture
def no_local_vlm(monkeypatch):
    """The box has no Ollama — the sandbox's situation, and the measured one."""

    async def unreachable(data: bytes, prompt: str = "", **kw: Any) -> str:
        raise ConnectionError("connection refused")

    monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", unreachable)


@pytest.fixture
def no_remote_model(monkeypatch):
    from robothor.engine import vision_fallback

    monkeypatch.setattr(vision_fallback, "configured_remote_model", lambda: "")


def _png(tmp_path, name="x.png", size=(40, 30)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return path


async def _view(args, ctx=None):
    from robothor.engine.tools.dispatch import _collect_handlers

    return await _collect_handlers()["view_image"](args, ctx)


class _FakeResponse:
    """What an OpenAI-compatible provider hands back, minus the network."""

    def __init__(self, text: str) -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = type("U", (), {"prompt_tokens": 400, "completion_tokens": 40})()


# ── the ladder ──────────────────────────────────────────────────────────────


class TestViewImageClimbsTheSameLadder:
    async def test_the_local_model_answers_before_a_paid_one_is_dialled(
        self, tmp_path, monkeypatch
    ) -> None:
        """The local VLM is this box's GPU and costs nothing. It goes first."""
        from robothor.engine import vision_fallback

        dialled: list[str] = []

        async def local(data: bytes, prompt: str = "", **kw: Any) -> str:
            return "a red rectangle"

        async def remote(*a: Any, **k: Any):
            dialled.append("remote")
            raise AssertionError("the remote model must not be dialled")

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", local)
        monkeypatch.setattr(vision_fallback, "remote_answer", remote)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert out["seen_by"] == "vision-model"
        assert out["backend"] == "local"
        assert out["description"] == "a red rectangle"
        assert not dialled

    async def test_the_remote_model_answers_when_the_local_one_is_unreachable(
        self, tmp_path, monkeypatch, no_local_vlm
    ) -> None:
        """The measured failure, fixed: a container with no Ollama and a
        configured remote vision model gets a description, not an error."""
        from robothor.engine import vision_fallback

        seen: list[dict[str, Any]] = []

        async def fake_completion(**kwargs: Any):
            seen.append(kwargs)
            return _FakeResponse("a bar chart of aid flow")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_completion)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert "error" not in out, out
        assert out["seen_by"] == "vision-model"
        assert out["backend"] == "remote"
        assert out["model"] == DECLARED_VISION_MODEL
        assert out["description"] == "a bar chart of aid flow"
        assert out["tokens"] == 440
        assert out["cost_usd"] > 0
        # The picture went to the provider as an image block, not as a filename.
        assert seen and seen[0]["model"] == DECLARED_VISION_MODEL

    async def test_an_undeclared_remote_model_is_not_dialled(
        self, tmp_path, monkeypatch, no_local_vlm
    ) -> None:
        """The same gate `analyze_image` applies. A model the registry has
        never heard of is a configuration mistake, not a risk to take."""
        from robothor.engine import vision_fallback

        async def fake_completion(**kwargs: Any):
            raise AssertionError("an undeclared model must not be dialled")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_completion)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: "acme/never-heard-of-it-v9"
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert out["seen_by"] == "nobody"
        assert "accepts_images" in out["error"]

    async def test_nobody_looked_is_said_plainly_when_every_rung_is_out(
        self, tmp_path, no_local_vlm, no_remote_model
    ) -> None:
        """The honest error, with BOTH rungs named — the agent has to be able
        to tell "no vision anywhere" from "the local one is down"."""
        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert out["seen_by"] == "nobody"
        assert "image_base64" not in out
        assert "description" not in out
        error = out["error"]
        assert "ROBOTHOR_VISION_REMOTE_MODEL" in error
        assert "ConnectionError" in error

    async def test_a_remote_failure_after_a_local_one_names_both(
        self, tmp_path, monkeypatch, no_local_vlm
    ) -> None:
        """The hostile case: a text-only primary, a declared remote model, and
        the provider unreachable. Nothing is invented and both rungs are
        named, because "the remote model is down" and "you configured none"
        need different fixes."""
        from robothor.engine import vision_fallback

        async def fake_completion(**kwargs: Any):
            raise TimeoutError("read timeout")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_completion)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert out["seen_by"] == "nobody"
        assert "description" not in out
        assert "ConnectionError" in out["error"]
        assert "TimeoutError" in out["error"]


# ── provenance ──────────────────────────────────────────────────────────────


class TestEveryVisionResultSaysWhereItCameFrom:
    async def test_view_image_says_so_when_the_agent_looked_itself(self, tmp_path) -> None:
        token = mr.note_active_model("openrouter/anthropic/claude-sonnet-4.6")
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        from robothor.engine.vision_fallback import PROVENANCE, PROVENANCE_NOTE

        assert out["provenance"] == PROVENANCE
        assert PROVENANCE_NOTE in out["provenance_note"]

    async def test_view_image_says_so_on_the_fallback_ladder(self, tmp_path, monkeypatch) -> None:
        from robothor.engine.vision_fallback import PROVENANCE

        async def local(data: bytes, prompt: str = "", **kw: Any) -> str:
            return "a red rectangle"

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", local)
        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))})
        finally:
            mr.reset_active_model(token)

        assert out["provenance"] == PROVENANCE

    async def test_analyze_image_says_so_for_the_whole_batch(self, tmp_path, monkeypatch) -> None:
        from robothor.engine import vision_batch
        from robothor.engine.tools.dispatch import _collect_handlers
        from robothor.engine.vision_fallback import PROVENANCE, PROVENANCE_NOTE

        async def local(data: bytes, prompt: str = "", **kw: Any) -> str:
            return "ANSWER: a chart\nWHY: bars and an axis"

        monkeypatch.setattr(vision_batch, "describe_image_bytes", local)
        monkeypatch.setattr(
            vision_batch, "resolve_backend", lambda: (vision_batch.Backend("local", "test-vlm"), "")
        )
        ctx = type("Ctx", (), {"workspace": str(tmp_path), "run_id": "r1", "agent_id": "probe"})()
        out = await _collect_handlers()["analyze_image"](
            {"paths": [str(_png(tmp_path))], "question": "what is this?"}, ctx
        )

        assert out["provenance"] == PROVENANCE
        assert PROVENANCE_NOTE in out["provenance_note"]


# ── the guidance ────────────────────────────────────────────────────────────


class TestObservedEvidenceOutranksNames:
    def test_the_instruction_contract_carries_the_sentence_exactly_once(self) -> None:
        from robothor.engine.prompts import EVIDENCE_OUTRANKS_NAMES, behavioral_rules

        assert behavioral_rules().count(EVIDENCE_OUTRANKS_NAMES) == 1

    def test_the_sentence_is_general_and_not_about_images(self) -> None:
        """It has to carry files, records and labels. An image-only wording
        teaches the agent nothing about a CSV whose column header lies."""
        from robothor.engine.prompts import EVIDENCE_OUTRANKS_NAMES

        lowered = EVIDENCE_OUTRANKS_NAMES.lower()
        assert "image" not in lowered
        assert "photo" not in lowered
        assert "observation" in lowered

    def test_both_vision_tools_repeat_it_where_the_agent_reads_it(self) -> None:
        from robothor.engine.prompts import EVIDENCE_OUTRANKS_NAMES
        from robothor.engine.tools.schemas import get_engine_schemas

        schemas = get_engine_schemas()
        for name in ("view_image", "analyze_image"):
            description = schemas[name]["function"]["description"]
            assert EVIDENCE_OUTRANKS_NAMES in description, name

    def test_analyze_image_points_at_choices_as_the_way_to_classify(self) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas

        schemas = get_engine_schemas()
        description = schemas["analyze_image"]["function"]["description"]
        assert "choices" in description
        assert "reason" in description

    def test_the_vision_descriptions_say_where_an_answer_comes_from(self) -> None:
        from robothor.engine.tools.schemas import get_engine_schemas
        from robothor.engine.vision_fallback import PROVENANCE_NOTE

        schemas = get_engine_schemas()
        for name in ("view_image", "analyze_image"):
            blob = str(schemas[name]["function"])
            assert PROVENANCE_NOTE in blob, name
