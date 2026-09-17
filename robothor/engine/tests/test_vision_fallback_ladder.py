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


def _ctx(tmp_path):
    """A run context with a workspace — what production always passes.

    Round 1, I-4: `view_image` now judges containment on the resolved path, so
    a test calling it with no workspace is testing a shape production never
    uses (and gets refused, correctly, because tmp_path is not in the
    instance's workspace).
    """
    return type("Ctx", (), {"workspace": str(tmp_path), "run_id": "r1", "agent_id": "probe"})()


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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
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


# ── round 1: the review's findings ──────────────────────────────────────────


class TestTheLocalRungCannotEatTheToolDeadline:
    """Review I-1. The local rung took `images.VISION_TIMEOUT_SECONDS` (120 s)
    and the remote rung 90 s, against a `view_image` tool deadline of 120 s —
    so a box whose Ollama is up but SLOW (a cold model on a busy GPU, the
    ordinary case) burned the whole budget on the rung that was going to fail
    and was cancelled before the remote rung was ever tried. The agent then
    saw a tool timeout, which reads exactly like the "this instance has no
    vision" this work exists to abolish.

    The fast failures — connection refused, nothing configured — return in
    milliseconds, which is why every probe missed it.
    """

    async def test_a_slow_local_model_does_not_starve_the_remote_rung(
        self, tmp_path, monkeypatch
    ) -> None:
        import asyncio
        import time

        from robothor.engine import vision_fallback

        async def slow(data: bytes, prompt: str = "", *, timeout: float = 120.0, **kw: Any) -> str:
            await asyncio.sleep(timeout + 5.0)
            raise AssertionError("the local rung was not bounded")

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", slow)
        monkeypatch.setattr(vision_fallback, "look_timeout", lambda: 0.05)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        async def fake_completion(**kwargs: Any):
            return _FakeResponse("a bar chart")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_completion)

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        started = time.monotonic()
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        assert out["backend"] == "remote", out
        assert time.monotonic() - started < 2.0, "the local rung ran past its budget"

    def test_two_rungs_fit_inside_the_tool_deadline(self) -> None:
        """The arithmetic, pinned. `view_image` is not in LONG_RUNNING_TOOLS,
        so it gets the agent default tool timeout; both rungs plus overhead
        have to fit under it or the ladder has a rung it can never reach."""
        from robothor.engine.vision_fallback import look_timeout

        assert look_timeout() * 2 < 120.0


class TestEachRungSaysWhichKindOfFailureItWas:
    """Review I-2. Every exception on the local rung read as "unavailable
    (RuntimeError)" — including the one `describe_image_bytes` raises when
    nothing is configured — so the module's own promise (that "the remote model
    timed out" and "you configured no remote model" need different fixes) held
    on one rung and was inverted on the other. The message was thrown away
    too, so an expired key, a 402 and a network partition all read alike.
    """

    async def test_an_unconfigured_local_model_is_not_reported_as_a_failure(
        self, tmp_path, monkeypatch, no_remote_model
    ) -> None:
        from robothor.engine import vision_fallback

        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "")

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        error = out["error"]
        assert "no local vision model is configured (ROBOTHOR_VISION_MODEL)" in error
        assert "RuntimeError" not in error, "a missing setting is not a runtime failure"

    async def test_an_unreachable_local_model_keeps_its_message(
        self, tmp_path, monkeypatch, no_remote_model
    ) -> None:
        from robothor.engine import vision_fallback

        async def refused(data: bytes, prompt: str = "", **kw: Any) -> str:
            raise ConnectionError("connection refused on port 11434")

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", refused)
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "a-local-vlm")

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        assert "ConnectionError" in out["error"]
        assert "connection refused on port 11434" in out["error"]

    async def test_a_remote_failure_keeps_its_message(
        self, tmp_path, monkeypatch, no_local_vlm
    ) -> None:
        from robothor.engine import vision_fallback

        async def fake_completion(**kwargs: Any):
            raise RuntimeError("402 insufficient credit for this key")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", fake_completion)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        assert "402 insufficient credit" in out["error"]

    async def test_a_rung_message_is_capped(self, tmp_path, monkeypatch, no_remote_model) -> None:
        """A provider that answers a failure with a page of HTML must not put
        the page into the agent's context under an error key."""
        from robothor.engine import vision_fallback

        async def verbose(data: bytes, prompt: str = "", **kw: Any) -> str:
            raise RuntimeError("x" * 5000)

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", verbose)
        monkeypatch.setattr(vision_fallback, "configured_local_model", lambda: "a-local-vlm")

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        assert len(out["error"]) < 1000, "a rung's message is quoted, not pasted"


class TestTheSameGuardsAsItsSibling:
    """Review I-4. `view_image` applied neither the secret-path rule nor the
    workspace rule that `analyze_image` applies to the same bytes — and this
    work routes those bytes to a third-party provider on a text-only primary,
    where they used to stop at the on-box Ollama. The reviewer uploaded a file
    from under the instance's secrets directory.

    One helper answers for both tools, so the two cannot drift into two
    different ideas of what a vision tool may read.
    """

    async def _both(self, tmp_path, path, monkeypatch):
        from robothor.engine import vision_fallback
        from robothor.engine.tools.dispatch import _collect_handlers

        dialled: list[str] = []

        async def local(data: bytes, prompt: str = "", **kw: Any) -> str:
            dialled.append("local")
            return "should never be reached"

        async def remote(**kwargs: Any):
            dialled.append("remote")
            raise AssertionError("a refused file must never reach a provider")

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", local)
        monkeypatch.setattr(vision_fallback, "pooled_acompletion", remote)
        monkeypatch.setattr(
            vision_fallback, "configured_remote_model", lambda: DECLARED_VISION_MODEL
        )

        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            looked = await _view({"path": str(path)}, _ctx(tmp_path))
            batched = await _collect_handlers()["analyze_image"](
                {"paths": [str(path)], "question": "what is this?"}, _ctx(tmp_path)
            )
        finally:
            mr.reset_active_model(token)
        return looked, batched["results"][0], dialled

    async def test_a_secrets_file_is_refused_by_both_with_the_same_words(
        self, tmp_path, monkeypatch
    ) -> None:
        # A real secrets directory by `secret_paths`' own rule, so the test
        # exercises the shared helper rather than a fixture invented to match
        # it. A screenshot of a key is still a key.
        secrets = tmp_path / ".ssh"
        secrets.mkdir()
        path = _png(secrets, name="key_on_screen.png")

        looked, row, dialled = await self._both(tmp_path, path, monkeypatch)

        assert "error" in looked, looked
        assert looked["error"] == row["error"]
        assert not dialled, "a refused file must not reach any backend"

    async def test_a_file_outside_the_workspace_is_refused_by_both(
        self, tmp_path, monkeypatch
    ) -> None:
        from robothor.engine import vision_fallback
        from robothor.engine.tools.dispatch import _collect_handlers

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        path = _png(outside, name="somebody_elses.png")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async def remote(**kwargs: Any):
            raise AssertionError("a refused file must never reach a provider")

        monkeypatch.setattr(vision_fallback, "pooled_acompletion", remote)
        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            looked = await _view({"path": str(path)}, _ctx(workspace))
            batched = await _collect_handlers()["analyze_image"](
                {"paths": [str(path)], "question": "what is this?"}, _ctx(workspace)
            )
        finally:
            mr.reset_active_model(token)

        assert "outside the workspace" in looked["error"]
        assert looked["error"] == batched["results"][0]["error"]

    async def test_a_symlink_pointing_out_of_the_tree_is_refused(self, tmp_path) -> None:
        """Containment is judged on the RESOLVED path, as the sibling's is."""
        outside = tmp_path / "outside"
        outside.mkdir()
        real = _png(outside, name="real.png")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        link = workspace / "innocent.png"
        link.symlink_to(real)

        token = mr.note_active_model("openrouter/anthropic/claude-sonnet-4.6")
        try:
            looked = await _view({"path": str(link)}, _ctx(workspace))
        finally:
            mr.reset_active_model(token)

        assert "outside the workspace" in looked["error"]

    async def test_an_ordinary_file_in_the_workspace_still_works(self, tmp_path) -> None:
        token = mr.note_active_model("openrouter/anthropic/claude-sonnet-4.6")
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)
        assert out["seen_by"] == "primary"


class TestProvenanceIsClaimedOnlyWhereSomethingLooked:
    """Review M-1. `provenance` was set before the rung was chosen, so a
    `seen_by: nobody` result shipped "answers come from the model looking at
    the image content" beside an error saying nobody looked — while the
    earlier refusals carried none at all. It was both over- and
    under-applied. The rule now: provenance describes an ANSWER, so it is
    present exactly where there is one.
    """

    async def test_nothing_looked_claims_no_provenance(
        self, tmp_path, no_local_vlm, no_remote_model
    ) -> None:
        token = mr.note_active_model(TEXT_ONLY_PRIMARY)
        try:
            out = await _view({"path": str(_png(tmp_path))}, _ctx(tmp_path))
        finally:
            mr.reset_active_model(token)

        assert out["seen_by"] == "nobody"
        assert "provenance" not in out
        assert "provenance_note" not in out

    async def test_a_refused_path_claims_no_provenance(self, tmp_path) -> None:
        out = await _view({"path": str(tmp_path / "absent.png")}, _ctx(tmp_path))
        assert "no such file" in out["error"]
        assert "provenance" not in out

    async def test_a_batch_that_answered_nothing_claims_no_provenance(
        self, tmp_path, monkeypatch
    ) -> None:
        from robothor.engine import vision_batch
        from robothor.engine.tools.dispatch import _collect_handlers

        async def broken(data: bytes, prompt: str = "", **kw: Any) -> str:
            raise RuntimeError("the backend is down")

        monkeypatch.setattr(vision_batch, "describe_image_bytes", broken)
        monkeypatch.setattr(
            vision_batch, "resolve_backend", lambda: (vision_batch.Backend("local", "test-vlm"), "")
        )
        out = await _collect_handlers()["analyze_image"](
            {"paths": [str(_png(tmp_path))], "question": "what is this?"}, _ctx(tmp_path)
        )

        assert out["analyzed"] == 0
        assert "provenance" not in out


class TestTheEvidenceSentenceIsAboutTheSameItem:
    """Review I-3. As written the rule told every agent to prefer any
    content-reading tool over any name — including when the tool looked at a
    DIFFERENT file from the one the name identified, which both vision tools
    can do deliberately (an extension miss substitutes a same-stem sibling).
    """

    def test_the_sentence_binds_the_observation_to_the_item(self) -> None:
        from robothor.engine.prompts import EVIDENCE_OUTRANKS_NAMES

        assert "of that same item" in EVIDENCE_OUTRANKS_NAMES

    def test_both_descriptions_still_fit_the_search_cap(self) -> None:
        from robothor.engine.tools.registry import ToolRegistry
        from robothor.engine.tools.schemas import get_engine_schemas

        schemas = get_engine_schemas()
        for name in ("view_image", "analyze_image"):
            description = schemas[name]["function"]["description"]
            assert len(description) <= ToolRegistry._SEARCH_DESC_MAX, (name, len(description))
