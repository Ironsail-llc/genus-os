"""One question, many pictures, answered out of band.

Measured 2026-09-16. On the WildClawBench Productivity task that hands an
agent a hundred photographs and asks it to categorise them, the competing
harness called its own vision tool a hundred times and scored **0.992**; this
engine called ``view_image`` four times and scored **0.424**. The sub-metric
is the one that says why: classification accuracy 0.99 against 0.28, where
five classes make 0.20 the score for guessing. It was guessing — from
filenames.

The difference is not the model, it is where the picture goes. ``view_image``
puts the image into the PRIMARY model's context (see
``tools/handlers/images.py``): one turn per image, the image tokens stay in
the conversation for the rest of the run, and the marginal cost of the tenth
look is paid on every call after it. An agent that has learned looking is
expensive stops looking. The competing tool is out of band — a separate
vision call per image, only the text answer returns — so it is cheap enough
to loop, and looping is what the task rewards.

So ``analyze_image`` takes a LIST of paths and ONE question, fans the vision
calls out with a bounded concurrency, and returns text. Nothing it returns
carries ``image_base64`` or ``image_mime``, which is the convention
``session.py`` keys its image-block emission on: an out-of-band tool that
quietly refilled the context with pictures would be ``view_image`` with extra
steps.

What it will not do
-------------------
* **Show a model a picture it cannot see.** The remote backend is checked
  against ``model_registry.image_capability`` first — the same honesty
  ``view_image`` learned in #578 — and falls back to the local VLM rather
  than posting an image block a provider will 404.
* **Read a file outside the workspace, or a credentials file.** Containment is
  judged on the RESOLVED path, so a symlink pointing out of the tree is
  refused; the secret-path rule is asked BEFORE the filesystem, so a refusal
  does not reveal whether the file exists.
* **Let one image take the batch down.** A backend that hangs, raises, or
  returns nothing marks that image and no other. A whole-call deadline bounds
  the case where the backend hangs for EVERY image: at 200 paths, four at a
  time, a per-image timeout alone would keep a dead backend alive for over an
  hour inside a single tool call.
* **Decode a 50 MB file to find out it was too big.** The size ceiling is a
  ``stat``, before Pillow is handed anything.
* **Fill the context it exists to keep empty.** Two hundred rows serialise to
  ~40,000 characters of short answers and ~432,000 of long ones (measured).
  Past a budget the totals and the first rows come back inline and the whole
  table goes to a file under the workspace. The session's own offloading does
  not cover this: ``tool_offload_threshold`` defaults to 0, and the manifest
  this change is measured on does not set it.

Why an answer comes with its reason
-----------------------------------
Measured again on 2026-09-17, on the same task. This time the agent DID call
the batch: 99 of 100 images answered, **92.9% of them correctly**, for $0.0145
and 29 s of model time. Copied verbatim into folders those answers were worth
0.936. It scored **0.432** — because it threw every one of them away.

The task's filenames are a deliberate derangement: ``brain_mri_scan.png`` is a
bar chart about Swedish aid flow, and filename-semantics agree with the truth
on **0 of 100** files. The tool returned ``{"answer": "3"}``. Facing a naked
digit against a confident-looking name, the agent wrote *"the vision model is
giving unreliable results"* — citing three examples, two of which were correct
— and hand-wrote a filename table with no vision input at all.

A bare token is unauditable, so it loses to any prior that looks like
evidence. The competing harness returned a paragraph and therefore detected the
trap in one turn. So this returns the active ingredient without the paragraph's
cost:

* ``choices`` makes the contract *"a label from this set"* rather than *"a
  string"*. The row carries a validated ``choice``, in the CALLER's spelling;
  an off-list reply is re-asked once and then reported as that row's ``error``
  — never prefix-matched, never squeezed into the nearest label.
* ``reason`` rides on every answered row: one capped sentence of what the model
  saw. ``{"choice": "1", "reason": "horizontal bar chart of aid flow from
  Sweden"}`` is not a thing an agent talks itself out of believing.
* A spilled result also carries a ``sample`` spanning the DISTINCT answers, so
  the spot-check is a glance rather than a ``read_file`` round.
* And the result says, in words, that a filename is a claim about a picture
  and a reason is what was in it. ``prompts.py`` rule 18 says the same.

What the deadline bounds, exactly
---------------------------------
It bounds when new work STARTS. Two things inside a round cannot be cancelled
and so set the overshoot: ``asyncio.to_thread`` running Pillow (a thread
finishes its decode whatever the loop wants — 40 copies of a 7000x7000 PNG
pushed a 2.0 s deadline to 2.48 s) and a backend coroutine that swallows
``CancelledError`` (``wait_for`` cancels, then *waits*; a stubborn one pushed
2.0 s to 5.51 s, and the answers it produced after its timeout are returned,
because an answer is an answer). Both are bounded by ONE in-flight round —
``max_concurrency`` images — because nothing new is started past the deadline.
That is the guarantee: deadline plus one round, not deadline exactly. It is
pinned by ``test_analyze_image.py::TestTheWholeCallDeadline``.

What containment here is, and is not
------------------------------------
It is a SCOPE, not an exfiltration boundary, and the difference is worth
stating because the rule looks like ``attachment_gate``'s and is not doing the
same job. There, a file's bytes leave the box to a person; here they reach a
provider only when a remote backend is configured, and the agent asking could
have copied the same file into the workspace with ``exec`` first. So this
refuses a path that wanders out of the tree — the honest reading of "analyze
the images in my workspace" — and does not pretend to be the control that
stops a determined agent reading a file it is already allowed to read. The
rung that matters is the secret-path one, which is shared with ``read_file``
and ``exec`` and is about credentials, not location.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robothor.engine.pooled_completion import acompletion as pooled_acompletion
from robothor.engine.tools.constants import MAX_TOOL_OUTPUT_CHARS
from robothor.engine.tools.handlers.images import (
    UnsupportedImageError,
    _same_stem_images,
    describe_image_bytes,
    prepare_image_bytes,
)
from robothor.engine.vision_contract import (
    MAX_CHOICE_CHARS,
    MAX_QUOTED_REPLY_CHARS,
    Contract,
    Reply,
    build_contract,
    contract_suffix,
    correction_suffix,
    parse_reply,
)

logger = logging.getLogger(__name__)

#: The most images one call may take. Two hundred is what the measured
#: behaviour needs (the task that exposed this gap holds ~100 files) and a
#: bound the run can survive: at the default concurrency and timeout, a
#: completely dead backend still ends inside the batch deadline below.
MAX_PATHS = 200

#: Vision calls in flight at once when the operator has not said otherwise.
#: Four is what a single local VLM on one GPU serves without queueing into its
#: own timeout. The SETTING that overrides this is a ceiling, not a default —
#: see :func:`_clamp_concurrency` — so an agent can ask for fewer and never for
#: more.
DEFAULT_CONCURRENCY = 4

#: The platform's own ceiling, above which no setting or request goes. An agent
#: that asks for 500 is not making a considered judgement about the provider's
#: rate limit.
MAX_CONCURRENCY = 16

#: Seconds one image gets before it is marked timed out and the batch moves on.
DEFAULT_TIMEOUT_SECONDS = 90.0

#: Seconds the whole call gets. See the module docstring: the per-image timeout
#: alone does not bound a backend that hangs for every image.
DEFAULT_DEADLINE_SECONDS = 600.0

#: Biggest file this will decode. A provider will not accept it either, and
#: the point of checking is that a decompression bomb costs the same whether
#: the pixels are used or thrown away.
MAX_IMAGE_BYTES = 32 * 1024 * 1024

#: Longest question forwarded to the backend. A question is a question; a
#: novel pasted into this field is a way to pay for the primary model's
#: context in the vision model's.
MAX_QUESTION_CHARS = 2000

#: How much detail the provider is asked to render the image at. ``low`` by
#: default because the whole design is "cheap enough to call a hundred times";
#: the schema tells the agent to ask for ``high`` when small text or fine
#: detail decides the answer.
DEFAULT_DETAIL = "low"
_DETAIL_CHOICES = frozenset({"low", "high"})

#: Answer tokens one image gets. Enough for a paragraph of description or a
#: transcription; short enough that 200 of them do not become a document.
MAX_ANSWER_TOKENS = 1024

#: And the same bound on the way back, in characters. A model told to answer
#: briefly usually does; a model that decides to narrate would otherwise put
#: 200 essays into the context this tool exists to keep empty. Offloading
#: catches an oversized result only for an agent whose manifest sets
#: ``tool_offload_threshold``, which is off by default -- so the cap is here
#: too, where it does not depend on somebody's configuration.
MAX_ANSWER_CHARS = 2000
_TRUNCATION_MARK = " […truncated]"

#: Longest a row's ``reason`` may be. The reason exists to be READ — a glance
#: at five of them is the spot-check — so it is bounded far tighter than the
#: answer: 200 rows of a 2,000-character justification is the context this
#: module exists to keep empty, arriving under a different key.
MAX_REASON_CHARS = 120

#: Rows in the spot-check ``sample`` a spilled result carries inline. Chosen to
#: span the DISTINCT answers rather than to be the first N, because the first N
#: rows of a folder an agent is sorting are the first N rows of one label.
MAX_SAMPLE_ROWS = 5

#: And the bound on the WHOLE result, which the per-answer cap does not give:
#: 200 short yes/no rows serialise to ~40,000 characters (~10k tokens) and 200
#: capped ones to ~432,000 (~108k tokens), measured. A tool whose stated
#: purpose is "does not fill your context" cannot put 108k tokens in it.
#:
#: The default is deliberately just under ``tracking.MAX_TOOL_OUTPUT_CHARS``
#: (4000), where the step writer replaces an oversized ``tool_output``
#: wholesale with a flat head/tail string: above that cap the per-image
#: ``tokens``/``cost_usd`` rows stop being a record of anything. Same
#: relationship, same reason, as ``session._ASSISTANT_TURN_MAX_SERIALISED``.
DEFAULT_MAX_TOTAL_CHARS = 3500

#: The hard ceiling on that budget, whatever the setting says. The step writer
#: replaces any ``tool_output`` over ``tracking.MAX_TOOL_OUTPUT_CHARS`` with a
#: flat head/tail string, which destroys both the per-image ledger and the
#: ``results_file`` pointer in the run record — so a budget at or above that
#: number is a budget that defeats the feature it configures. The margin
#: absorbs the difference between what this module measures (the serialised
#: result) and what the writer measures (the same string), which should be
#: nothing, plus room for a wrapper key nobody has added yet.
#: ``test_the_inline_result_never_exceeds_the_step_writer_cap`` pins the
#: relationship, the way ``test_turn_cap_is_below_the_step_writer_cap`` pins
#: the assistant turn's.
_STEP_WRITER_MARGIN = 200

#: Read from the step writer's own constant rather than repeated as a literal:
#: the two numbers are the same number, and a copy is a copy that drifts.
MAX_INLINE_CHARS = MAX_TOOL_OUTPUT_CHARS - _STEP_WRITER_MARGIN

#: Where the full table goes when the result does not fit. Under the
#: workspace, so the agent can ``read_file`` it back; under ``.robothor/``, so
#: it is not mistaken for a deliverable; not under ``.robothor/secret*``, which
#: is the prefix ``secret_paths`` refuses.
SPILL_DIRNAME = ".robothor/analyze_image"


@dataclass(frozen=True)
class Backend:
    """Which vision model answers, and how it is dialled.

    ``kind`` is ``"remote"`` (an OpenAI-compatible provider, images as data
    URIs) or ``"local"`` (this box's Ollama VLM). Two code paths, one because
    the sandbox has no Ollama and one because the box should not have to pay a
    provider to look at its own screenshots.

    ``note`` is how a fallback tells the truth. A batch that quietly answered
    from a different model than the operator configured is a result the agent
    cannot reason about, so the sentence rides on the backend and lands in the
    result.
    """

    kind: str
    model: str
    note: str = ""


@dataclass(frozen=True)
class Resolved:
    """What one requested path turned into: a file to read, or why not.

    ``shown`` is the path the row reports whatever happened, so an agent can
    line its request up against the results by string. ``substituted_for`` is
    set when the agent asked for an extension that missed and exactly one image
    beside it shared the stem — the row says both, because an agent that is not
    told it was handed a different file believes it looked at the one it named.
    """

    path: Path | None
    refusal: str
    shown: str
    substituted_for: str = ""


def _settings() -> Any:
    from robothor.settings import get_settings

    return get_settings()


def _configured_remote_model() -> str:
    """``ROBOTHOR_VISION_REMOTE_MODEL``, or empty when the box uses Ollama."""
    try:
        return str(_settings().providers.vision_remote_model or "").strip()
    except Exception:  # noqa: BLE001 - a missing config is "not configured", not a crash
        logger.debug("settings unavailable while resolving the remote vision model")
        return ""


def _configured_local_model() -> str:
    """``ROBOTHOR_VISION_MODEL`` — the same field ``view_image`` reads."""
    try:
        return str(_settings().ollama.vision_model or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("settings unavailable while resolving the local vision model")
        return ""


def _configured_concurrency() -> int:
    try:
        return int(_settings().providers.vision_batch_concurrency)
    except Exception:  # noqa: BLE001
        return DEFAULT_CONCURRENCY


def _per_image_timeout() -> float:
    try:
        return float(_settings().providers.vision_batch_timeout)
    except Exception:  # noqa: BLE001
        return DEFAULT_TIMEOUT_SECONDS


def _max_total_chars() -> int:
    """The inline budget, clamped so a setting cannot defeat the design.

    Two ways it could, both found by re-review:

    * **0 or a negative number.** The guard used to be ``if budget > 0``, so
      zero turned the bound OFF and returned the 62,656-character result the
      whole spill exists to prevent — the opposite of what an operator setting
      0 to mean "always spill" would expect. Non-positive means "the default",
      the same reading :func:`_clamp_concurrency` gives a non-positive request.
    * **A number above the step writer's cap.** The setting's own text says the
      default sits *just under* 4,000, which invites an operator who wants a
      few more inline rows to set 3,900 — at which point ``tracking``
      truncates the whole ``tool_output`` and the per-image ledger this design
      protects is gone, silently. So the ceiling is enforced here rather than
      described in a sentence somebody has to read.
    """
    try:
        configured = int(_settings().providers.vision_batch_max_chars)
    except Exception:  # noqa: BLE001
        configured = DEFAULT_MAX_TOTAL_CHARS
    if configured <= 0:
        configured = DEFAULT_MAX_TOTAL_CHARS
    return min(configured, MAX_INLINE_CHARS)


def _batch_deadline() -> float:
    try:
        return float(_settings().providers.vision_batch_deadline)
    except Exception:  # noqa: BLE001
        return DEFAULT_DEADLINE_SECONDS


def resolve_backend() -> tuple[Backend | None, str]:
    """``(backend, refusal)`` — exactly one of the two is meaningful.

    The remote model must be DECLARED ``accepts_images`` in the registry. Not
    "not declared blind" — declared able. The first version of this gate asked
    ``!= "rejects"``, which passes every model nobody has written an entry for,
    and a hostile review set that setting to an undeclared model and watched
    the batch dial it: OpenRouter answered ``404 No endpoints found that
    support image input`` per image. That is exactly the #578 failure, reached
    two hundred times per call, on the one setting whose entire job is to name
    a vision model.

    ``view_image`` is right to keep dialling on ``unknown`` and this is right
    not to, for a reason worth stating: there the model is whatever the fleet
    happens to be running and one image is at stake, so the result carries a
    note and the agent learns. Here an operator has explicitly named a model to
    be *the vision backend*, so "we have never heard of it" is a configuration
    mistake to report, not a risk to take 200 times.

    When a local VLM is configured the batch falls back to it (the backend
    carries a ``note`` saying so); when it is not, this refuses rather than
    pretending.
    """
    remote = _configured_remote_model()
    local = _configured_local_model()
    if remote:
        from robothor.engine.model_registry import image_capability

        capability = image_capability(remote)
        if capability == "accepts":
            return Backend("remote", remote), ""
        why = (
            "does not accept images"
            if capability == "rejects"
            else "is not declared `accepts_images` in the engine's model registry"
        )
        if not local:
            return None, (
                f"refused: the configured remote vision model ({remote}) {why}. Add a "
                "registry entry declaring `accepts_images=True` for it, name a declared "
                "model in ROBOTHOR_VISION_REMOTE_MODEL, or configure a local one with "
                "ROBOTHOR_VISION_MODEL."
            )
        logger.warning("remote vision model %s %s; falling back to the local VLM", remote, why)
        return (
            Backend(
                "local",
                local,
                note=(
                    f"the configured remote vision model ({remote}) {why}, so the local "
                    f"vision model ({local}) answered instead."
                ),
            ),
            "",
        )
    if local:
        return Backend("local", local), ""
    return None, (
        "refused: no vision model is configured. Set ROBOTHOR_VISION_MODEL (local, via "
        "Ollama) or ROBOTHOR_VISION_REMOTE_MODEL (a provider model the registry declares "
        "`accepts_images`)."
    )


def _workspace_root(workspace: str) -> Path | None:
    """The tree an image has to be inside, or None when there is no answer.

    The caller's ``ctx.workspace`` first, settings second — the idiom
    ``tools/handlers/attachments.py`` uses for the same question, and for the
    same reason: an unresolvable workspace refuses rather than defaulting to
    "anywhere on the filesystem".
    """
    if workspace:
        return Path(workspace).expanduser().resolve(strict=False)
    try:
        from robothor.settings.sources import workspace_path

        resolved = workspace_path()
        return resolved.resolve(strict=False) if resolved is not None else None
    except Exception:  # noqa: BLE001 - an unresolvable workspace refuses, never reads
        return None


def _resolve_one(raw: str, root: Path) -> Resolved:
    """The file one requested image names, or why there is not one.

    No backend call on a refusal. Order matters and mirrors
    ``attachment_gate.resolve_for_send``: containment on the RESOLVED path (so
    a symlink out of the tree is caught), then the secret-path name rule BEFORE
    any stat (so a refusal does not reveal whether the file exists), then
    shape, then size.

    ``shown`` exists because a refused row used to report the path as the agent
    spelled it while an answered row reported the resolved one, so an agent
    zipping its request to the results by path string got a mismatch on exactly
    the rows it needed to retry. Every row that got as far as resolving now
    reports the resolved path; a blank one has nothing to resolve.

    A missed EXTENSION is not a miss. ``view_image`` learned that in
    ``handlers/images.py::_same_stem_images`` — added because the recovery was
    costing Code Intelligence tasks — and this module shipped without importing
    it, so the same bug lived on in the sibling tool: ``retinal_scan.png`` came
    back "no such file" with ``retinal_scan.jpg`` sitting beside it. The rule is
    the one ``view_image`` already applies: exactly one candidate substitutes,
    two is a question for the agent rather than a coin flip for the tool. The
    substitute is looked for only in a directory strictly inside the workspace,
    so a path that resolves TO the root cannot reach the root's siblings, and it
    is put through the secret-path rule like any other file.
    """
    text = str(raw or "").strip()
    if not text:
        return Resolved(None, "path is required", str(raw))

    candidate = Path(text).expanduser()
    relative = not candidate.is_absolute()
    if relative:
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    shown = str(resolved)
    if root != resolved and root not in resolved.parents:
        return Resolved(
            None,
            (
                f"refused: {Path(text).name} resolves outside the workspace. Only images "
                "inside the workspace can be analyzed."
            ),
            shown,
        )

    from robothor.engine.secret_paths import is_secret_path, refusal_for

    if is_secret_path(resolved):
        return Resolved(None, refusal_for(resolved), shown)

    substituted_for = ""
    if not resolved.is_file():
        found = _substitute(resolved, root)
        if found.refusal or found.path is None:
            return Resolved(
                None, found.refusal or _no_such_file(text, shown, root, relative), shown
            )
        substituted_for, resolved = shown, found.path
        shown = str(resolved)
        if is_secret_path(resolved):
            return Resolved(None, refusal_for(resolved), shown)

    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return Resolved(
            None,
            (
                f"refused: {resolved.name} is too large to analyze ({size // (1024 * 1024)} MB; "
                f"the limit is {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
            ),
            shown,
        )
    return Resolved(resolved, "", shown, substituted_for)


def _substitute(resolved: Path, root: Path) -> Resolved:
    """The one image beside *resolved* sharing its stem, or why there is none."""
    if root not in resolved.parents:
        return Resolved(None, "", str(resolved))
    siblings = _same_stem_images(resolved)
    if len(siblings) == 1:
        return Resolved(siblings[0], "", str(siblings[0]))
    if siblings:
        names = ", ".join(sorted(s.name for s in siblings))
        return Resolved(
            None,
            (
                f"no such file: {resolved.name} — several images share that name "
                f"({names}). Ask for the one you want."
            ),
            str(resolved),
        )
    return Resolved(None, "", str(resolved))


def _no_such_file(text: str, shown: str, root: Path, relative: bool) -> str:
    """Why nothing was read, said usefully enough to fix on the next call.

    Batch 1 of the measured run passed fifty bare basenames, every one of which
    came back ``no such file: 3d_architectural_render.jpg`` — with no hint that
    a root join had been tried, or what root. The images were one directory
    down. Fifty rows of an error that does not say where it looked is a wasted
    round; saying it costs one sentence on the rows that already failed.
    """
    if not relative:
        return f"no such file: {text}"
    return (
        f"no such file: {text} — a relative path is joined to the workspace root "
        f"({root}), which gave {shown}. Pass the path read_file would take, or list "
        "the directory first."
    )


def _load(path: Path) -> tuple[bytes, str]:
    """``(bytes, mime)`` ready for a model. Blocking — always run in a thread.

    Pillow decoding is CPU-bound and synchronous. Four of these on the event
    loop would serialise the batch it exists to parallelise, and a large image
    would stall every other agent on the box while it decoded.
    """
    prepared = prepare_image_bytes(path)
    return prepared.data, prepared.mime


def _price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """What this call cost, from the registry's declared rates.

    Computed here rather than read from the provider's response: the registry
    is what every other cost number on this instance is derived from, and a
    tool result that reported a different basis would make the run total
    disagree with itself.
    """
    try:
        from robothor.engine.model_registry import get_model_limits

        limits = get_model_limits(model)
    except Exception:  # noqa: BLE001 - an unpriced model costs an unknown amount, not a crash
        return 0.0
    return (
        prompt_tokens * limits.input_cost_per_token
        + completion_tokens * limits.output_cost_per_token
    )


async def _remote_answer(
    backend: Backend, data: bytes, mime: str, question: str, detail: str, timeout: float
) -> tuple[str, int, float]:
    """``(answer, tokens, cost)`` from an OpenAI-compatible vision provider.

    Through the pooled client, never ``litellm.acompletion`` directly: a
    module that resolves its own key cannot rotate off a capped one, which is
    how a single retired credential kept being re-dialled for a whole day.
    """
    payload = base64.b64encode(data).decode("ascii")
    response = await pooled_acompletion(
        model=backend.model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{payload}", "detail": detail},
                    },
                ],
            },
        ],
        max_tokens=MAX_ANSWER_TOKENS,
        temperature=0.1,
        timeout=timeout,
    )
    answer = str(response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if not answer:
        raise RuntimeError("the vision model returned nothing")
    return (
        answer,
        prompt_tokens + completion_tokens,
        _price(backend.model, prompt_tokens, completion_tokens),
    )


#: What the vision model is told it is for. The literal, insistent form
#: ``view_image`` uses — a model that decides it "cannot access websites"
#: because the screenshot contains a URL has described nothing — with the
#: batch's own instruction to answer the question that was asked.
_SYSTEM_PROMPT = (
    "You are a vision system answering one question about one image. Answer the question "
    "directly and concretely, in at most a short paragraph. Read and transcribe any text "
    "that bears on the answer, exactly as shown. Never refuse, never say you cannot access "
    "websites or URLs — a URL in an image is text to be read, not a page to visit. If the "
    "image does not contain what was asked about, say so plainly rather than guessing."
)


@dataclass(frozen=True)
class Attempt:
    """What one image cost and what came back, parsed. ``reasked`` is True when
    the first reply named no allowed choice and a second was paid for."""

    reply: Reply
    tokens: int
    cost: float
    truncated: bool
    reasked: bool = False


async def _ask_once(
    backend: Backend,
    data: bytes,
    mime: str,
    question: str,
    detail: str,
    timeout: float,
    contract: Contract,
) -> Attempt:
    """One backend call, parsed and bounded. Raises on a backend that fails."""
    if backend.kind == "remote":
        raw, tokens, cost = await _remote_answer(backend, data, mime, question, detail, timeout)
    else:
        # The local VLM reports no usage and costs nothing: it is this box's GPU.
        raw = (await describe_image_bytes(data, question, timeout=timeout)).strip()
        tokens, cost = 0, 0.0
    if not raw:
        raise RuntimeError("the vision model returned nothing")
    parsed = parse_reply(raw, contract)
    answer, cut = _cap(parsed.answer, MAX_ANSWER_CHARS)
    reason, _ = _cap(parsed.reason, MAX_REASON_CHARS)
    return Attempt(Reply(answer, reason, parsed.rejected), tokens, cost, cut)


async def _answer_one(
    backend: Backend,
    data: bytes,
    mime: str,
    question: str,
    detail: str,
    timeout: float,
    contract: Contract,
) -> Attempt:
    """One image's answer, with the single re-ask a rejected label earns.

    The re-ask is for a WRONG answer, never for a thin one. A model that gave a
    valid label and skipped its WHY line has still answered; charging every
    image in the batch a second vision call because a weak backend does not
    emit a marker would undo the property the whole tool rests on. That row
    says ``reason_missing`` instead, so the gap is visible rather than filled in.
    """
    first = await _ask_once(
        backend, data, mime, question + contract_suffix(contract), detail, timeout, contract
    )
    if not first.reply.rejected:
        return first
    second = await _ask_once(
        backend,
        data,
        mime,
        question + correction_suffix(contract, first.reply.rejected),
        detail,
        timeout,
        contract,
    )
    return Attempt(
        second.reply,
        first.tokens + second.tokens,
        first.cost + second.cost,
        second.truncated,
        reasked=True,
    )


def _cap(answer: str, limit: int) -> tuple[str, bool]:
    """One string, bounded, and whether it was cut.

    The marker inside the string is for a reader; the flag is for a caller. An
    agent transcribing text off a hundred images needs to know WHICH rows it
    only half has, and asking it to substring-match a marker to find out is how
    a truncation goes unnoticed.
    """
    if len(answer) <= limit:
        return answer, False
    return answer[:limit].rstrip() + _TRUNCATION_MARK, True


async def _analyze_one(
    raw: str,
    *,
    backend: Backend,
    root: Path,
    question: str,
    detail: str,
    semaphore: asyncio.Semaphore,
    deadline: float,
    contract: Contract,
) -> dict[str, Any]:
    """One image's row of the result. Never raises — a row always comes back."""
    started = time.monotonic()
    found = _resolve_one(raw, root)
    resolved = found.path
    if resolved is None:
        return {"path": found.shown, "error": found.refusal, "ms": 0}

    async with semaphore:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "path": str(resolved),
                "error": (
                    "not analyzed — the batch ran out of time. Ask again with fewer "
                    "images, or a higher max_concurrency."
                ),
                "ms": int((time.monotonic() - started) * 1000),
            }
        timeout = min(_per_image_timeout(), remaining)
        try:
            data, mime = await asyncio.to_thread(_load, resolved)
        except UnsupportedImageError as exc:
            return {
                "path": str(resolved),
                "error": str(exc),
                "ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - Pillow raises widely on malformed input
            return {
                "path": str(resolved),
                "error": f"could not read as an image: {type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - started) * 1000),
            }

        try:
            attempt = await asyncio.wait_for(
                _answer_one(backend, data, mime, question, detail, timeout, contract),
                timeout=timeout,
            )
        except TimeoutError:
            return {
                "path": str(resolved),
                "error": f"the vision model timed out after {timeout:.0f}s on this image",
                "ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - one image's failure, reported as one row
            logger.warning("vision call failed for %s: %s", resolved.name, exc)
            return {
                "path": str(resolved),
                "error": f"{type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - started) * 1000),
            }

    return _row(attempt, resolved, found.substituted_for, backend, contract, started)


def _row(
    attempt: Attempt,
    resolved: Path,
    substituted_for: str,
    backend: Backend,
    contract: Contract,
    started: float,
) -> dict[str, Any]:
    """One answered image, as the agent reads it.

    A constrained row carries ``choice`` and a free one carries ``answer``, and
    neither carries both: the validated label IS the answer, and saying it twice
    doubles the one thing the inline budget is measured in. A label the model
    never gave is not written at all — that row is an ``error``, which is the
    difference between "give me a string" and "give me a label from this set".

    A rejected row carries no ``reason`` either. The sentence justifies a label
    the tool refused to record, so keeping it would put the unvalidated answer
    back one key over.
    """
    row: dict[str, Any] = {"path": str(resolved)}
    if substituted_for:
        row["resolved_from"] = substituted_for
    if attempt.reply.rejected:
        quoted = attempt.reply.rejected[:MAX_QUOTED_REPLY_CHARS].replace("\n", " ")
        row["error"] = (
            f"the vision model answered {quoted!r} twice, which is not one of the choices "
            f"({contract.listed}). Nothing is recorded for this image rather than a label "
            "the model did not give — look at it with view_image if it decides the task."
        )
    else:
        if contract.labels:
            row["choice"] = attempt.reply.answer
        else:
            row["answer"] = attempt.reply.answer
        if attempt.reply.reason:
            row["reason"] = attempt.reply.reason
        else:
            # Never manufactured out of the answer: an invented justification
            # is worse than a missing one, because the agent cannot tell them
            # apart.
            row["reason_missing"] = True
    row["model"] = backend.model
    row["ms"] = int((time.monotonic() - started) * 1000)
    if attempt.tokens:
        row["tokens"] = attempt.tokens
    if attempt.cost:
        row["cost_usd"] = round(attempt.cost, 6)
    if attempt.truncated:
        row["truncated"] = True
    if attempt.reasked:
        row["reasked"] = True
    return row


def _clamp_concurrency(requested: Any) -> int:
    """How many calls run at once: what was asked for, under the operator's cap.

    The setting is a CEILING, not a default. It read as a default for one
    draft, and a hostile review measured the consequence: with the setting at
    4, an agent asking for 12 got 12 and an agent asking for 500 got 16. The
    number exists because it is what a single local VLM on one GPU serves
    without queueing into its own timeout — an operator who lowers it to 1 on a
    small box means it, and an agent is in no position to overrule them.
    """
    ceiling = max(1, min(_configured_concurrency(), MAX_CONCURRENCY))
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        wanted = ceiling
    if wanted <= 0:
        wanted = ceiling
    return max(1, min(wanted, ceiling))


#: How many batches each run has spilled, so the next file gets the next
#: number. It was a ``glob`` of the whole directory, which paid for the
#: directory's own growth on every spill — a stat storm at a hundred thousand
#: files — and quietly assumed nothing was ever deleted from it: remove
#: ``run-x-2.json`` and the next spill for that run recomputed index 3 and
#: overwrote ``run-x-3.json``. A run lives in one process, so an in-process
#: counter is the authority, and the existence check below covers the rest.
_SPILL_COUNTS: dict[str, int] = {}

#: Beyond this many distinct runs the counter map is dropped whole rather than
#: grown forever in a daemon that never restarts. Restarting the count is
#: harmless: the loop below steps past a name already on disk.
_SPILL_COUNT_MEMORY = 512


def _spill_path(root: Path, run_id: str) -> Path:
    """Where this call's full table goes. Numbered, so a run keeps every batch."""
    directory = root / SPILL_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    stem = "".join(c for c in (run_id or "adhoc") if c.isalnum() or c in "-_") or "adhoc"
    if len(_SPILL_COUNTS) > _SPILL_COUNT_MEMORY:
        _SPILL_COUNTS.clear()
    index = _SPILL_COUNTS.get(stem, 0)
    while True:
        index += 1
        candidate = directory / f"{stem}-{index}.json"
        if not candidate.exists():
            _SPILL_COUNTS[stem] = index
            return candidate


#: How long a spilled table is kept. These are working files — an agent reads
#: one in the run that wrote it and never again — so the window is short. The
#: alternative is what shipped for one release: one ~67 KB JSON per big batch,
#: forever, in a directory no operator looks at and nothing prunes.
DEFAULT_SPILL_RETENTION_DAYS = 7


def prune_spill_files(
    *,
    retention_days: int | None = None,
    workspace: str | Path | None = None,
    now: float | None = None,
) -> int:
    """Delete spilled batch tables older than the retention window.

    Walks ``<workspace>/.robothor/analyze_image/`` and nothing else — the same
    rule ``attachments.prune_inbox`` follows, and for the same reason: a file
    an agent copied somewhere useful has left this tree and is not the prune's
    business. ``retention_days <= 0`` disables the prune rather than deleting
    everything, because "keep for zero days" is far likelier to be a
    misconfiguration than an instruction.
    """
    days = retention_days if retention_days is not None else _configured_spill_retention_days()
    if days <= 0:
        return 0
    root = _workspace_root(str(workspace) if workspace else "")
    if root is None:
        return 0
    directory = root / SPILL_DIRNAME
    if not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    for path in sorted(directory.glob("*.json")):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the sweep
            logger.warning("the analyze_image prune could not remove a file: %s", exc)
    return removed


def _configured_spill_retention_days() -> int:
    try:
        return int(_settings().providers.vision_batch_retention_days)
    except Exception:  # noqa: BLE001 - a prune must never break on config
        return DEFAULT_SPILL_RETENTION_DAYS


#: The sentence the measured failure turned on, said in every result.
#:
#: The agent had 99 answers that were 92.9% correct and a hundred filenames
#: that were a deliberate derangement — agreeing with the truth on 0 of 100
#: files — and it wrote "the vision model is giving unreliable results" and
#: shipped the filenames. Two of the three answers it cited as proof of
#: unreliability were correct. The rule is not bench-shaped: a mislabelled
#: download, a scan saved under whatever the scanner called it and a camera
#: roll are all the same shape, and this is the one place an agent reading the
#: answers is guaranteed to see it.
PIXELS_BEAT_NAMES = (
    "A filename is a claim; a reason is what was seen. Where they disagree, trust the look."
)


def _is_answered(row: dict[str, Any]) -> bool:
    """Whether a row holds an answer, under either of the two keys it can use."""
    return "choice" in row or "answer" in row


def _sample_row(row: dict[str, Any]) -> dict[str, Any]:
    """One preview row: which file, what was decided, and why. Nothing else.

    The model, the timing and the ledger are already inline as totals and in
    the spilled file; repeating them per sample row would spend the budget the
    sample is competing for on the one part of a row a spot-check never reads.
    """
    preview: dict[str, Any] = {"path": row["path"]}
    if "choice" in row:
        preview["choice"] = row["choice"]
    else:
        preview["answer"] = _cap(str(row.get("answer", "")), MAX_REASON_CHARS)[0]
    if row.get("reason"):
        preview["reason"] = row["reason"]
    return preview


def _pick_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Up to *limit* answered rows spanning the DISTINCT answers.

    Not the first N. An agent sorting a folder gets its rows in the order it
    passed the paths, which for a folder listing is alphabetical, which for a
    real folder is one label at a time — so the first five rows of a hundred
    are five looks at the same decision. Spanning the distinct answers is what
    makes one glance worth the characters it costs, and it is the cheap version
    of the competing harness's "let me verify a few more images to confirm".
    """
    by_answer: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = row.get("choice") or row.get("answer")
        if not isinstance(label, str):
            continue
        by_answer.setdefault(label[:MAX_CHOICE_CHARS], row)
        if len(by_answer) >= limit:
            break
    picked = list(by_answer.values())
    taken = {id(row) for row in picked}
    for row in rows:
        if len(picked) >= limit:
            break
        if id(row) not in taken and _is_answered(row):
            picked.append(row)
            taken.add(id(row))
    return [_sample_row(row) for row in picked]


def _fit(out: dict[str, Any], rows: list[dict[str, Any]], budget: int) -> int:
    """How many of *rows* fit in *budget* once the rest of *out* is counted.

    Measured on the serialised form, because the serialised form is what the
    session puts in the context and what the step writer measures against its
    own cap. Binary search rather than a row-by-row walk: 200 rows is 8
    ``json.dumps`` calls instead of 200.
    """
    probe = dict(out)
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high + 1) // 2
        probe["results"] = rows[:middle]
        if len(json.dumps(probe, default=str)) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


async def analyze_images(
    *,
    paths: Any,
    question: str,
    detail: str = "",
    choices: Any = None,
    max_concurrency: Any = None,
    workspace: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Answer *question* about every path in *paths*. Never returns a picture.

    The return shape is the contract the tool schema advertises: ``results`` in
    the order the paths were given, one row each, carrying ``choice`` (when
    *choices* constrained it), ``answer`` (when it did not) or ``error``, and
    never more than one of the three. Every answered row also carries the
    model's ``reason`` — the short account of what it saw — or says plainly that
    the backend gave none.

    A result too big for the budget keeps its totals, a label-spanning
    ``sample`` and its first rows inline, and writes the whole table to a file
    whose path it returns — see :data:`DEFAULT_MAX_TOTAL_CHARS` for why that is
    not left to the session's offloading.
    """
    if not isinstance(paths, (list, tuple)) or not paths:
        return {"error": f"paths is required: a list of 1–{MAX_PATHS} image paths"}
    if len(paths) > MAX_PATHS:
        return {
            "error": (
                f"refused: {len(paths)} images in one call, and the limit is {MAX_PATHS}. "
                "Split the work across calls."
            )
        }

    asked = question.strip()
    if not asked:
        return {"error": "question is required — say what you want to know about each image"}
    asked = asked[:MAX_QUESTION_CHARS]

    contract, bad_choices = build_contract(choices)
    if bad_choices:
        return {"error": bad_choices}

    notes: list[str] = []
    if len(question.strip()) > MAX_QUESTION_CHARS:
        notes.append(
            f"the question was cut to its first {MAX_QUESTION_CHARS} characters — the "
            "vision model only saw that much"
        )

    requested_detail = (detail or "").strip().lower()
    wanted_detail = requested_detail or DEFAULT_DETAIL
    if wanted_detail not in _DETAIL_CHOICES:
        notes.append(f"ignored detail={wanted_detail!r}; it must be one of low, high")
        wanted_detail = DEFAULT_DETAIL

    root = _workspace_root(workspace)
    if root is None:
        return {
            "error": (
                "refused: the workspace could not be resolved, so containment cannot be "
                "judged. Set ROBOTHOR_WORKSPACE."
            )
        }

    backend, refusal = resolve_backend()
    if backend is None:
        return {"error": refusal}
    if backend.note:
        notes.append(backend.note)
    if backend.kind == "local" and requested_detail:
        # Ollama's chat API has no `detail` knob, and the schema tells the
        # agent to ask for `high` when fine detail decides the answer. On a
        # local-backend instance that request is a no-op, and a no-op the
        # agent is not told about is the agent believing it looked closer.
        notes.append(
            f"detail={requested_detail!r} was ignored: the local vision model "
            f"({backend.model}) renders every image the same way. Use view_image if a "
            "detail the description misses decides the answer."
        )

    concurrency = _clamp_concurrency(max_concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    deadline = time.monotonic() + _batch_deadline()
    started = time.monotonic()

    results = await asyncio.gather(
        *(
            _analyze_one(
                str(raw),
                backend=backend,
                root=root,
                question=asked,
                detail=wanted_detail,
                semaphore=semaphore,
                deadline=deadline,
                contract=contract,
            )
            for raw in paths
        )
    )

    answered = [r for r in results if _is_answered(r)]
    failed = len(results) - len(answered)
    elapsed = time.monotonic() - started
    tokens = sum(int(r.get("tokens") or 0) for r in results)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in results)

    out: dict[str, Any] = {
        "question": asked,
        "model": backend.model,
        "backend": backend.kind,
        "analyzed": len(answered),
        "failed": failed,
        "results": list(results),
        "summary": (
            f"{len(answered)} of {len(results)} images answered by {backend.model} "
            f"in {elapsed:.1f}s ({concurrency} at a time)"
            + (f"; {failed} failed — see the per-image error" if failed else "")
            + ". "
            + PIXELS_BEAT_NAMES
        ),
    }
    if tokens:
        out["tokens"] = tokens
    if cost:
        # The runner adds a tool result's `cost_usd` to the run total, so an
        # out-of-band call that reported nothing would spend money invisibly.
        out["cost_usd"] = round(cost, 6)

    budget = _max_total_chars()
    if notes:
        out["note"] = " ".join(notes)
    if len(json.dumps(out, default=str)) > budget:
        _spill(out, list(results), notes=notes, root=root, run_id=run_id, budget=budget)
    return out


def _spill_sentence(total: int, shown: int, written: Path | None, sampled: bool = False) -> str:
    """What the agent is told about a table that went to disk.

    The path is NOT repeated here: it is already in ``results_file``, and a
    note that quotes it too spends a long workspace path twice out of a budget
    measured in hundreds of characters.

    ``sampled`` is not decoration. A note promising a spot-check beside a
    result that has none is the tool lying about its own shape, and the sample
    is the first thing the budget takes back — so the probe measures the widest
    sentence and the caller is told which one it actually got.
    """
    preview = f"the first {shown} are here" if shown else "none of them fit here"
    spot = ", sample spans the distinct answers" if sampled else ""
    if written is not None:
        return (
            f"{total} rows did not fit, so {preview}{spot}, and all {total} — answer, "
            "reason, tokens and cost — are in results_file. Work over that file with "
            "exec (jq or python) rather than reading it back whole; do not ask about "
            "these images again."
        )
    return (
        f"{total} rows did not fit and the table could not be written to disk, so "
        f"{preview}. The totals above cover all {total}. Ask about the rest in smaller "
        "batches, or raise ROBOTHOR_VISION_BATCH_MAX_CHARS; do not repeat this call whole."
    )


def _spill(
    out: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    notes: list[str],
    root: Path,
    run_id: str,
    budget: int,
) -> None:
    """Move the full table to a file, leave the totals and a preview. Mutates *out*.

    The totals stay inline whatever happens — the money and the counts are what
    a caller reads without opening anything — and so does ``results_file``, so
    an agent that wants row 147 knows where to get it.

    The fitting is done against the REAL note. It used to probe with a
    400-character placeholder and then join the actual notes afterwards, which
    spends the difference straight out of the budget after the arithmetic is
    finished: four notes together measure 1,025 characters, and a re-review
    found the result 516 over at a budget an operator could plausibly set. A
    control with tests certifying it that does not hold is the failure this
    project keeps re-learning, so the probe now carries the widest note that
    can be returned and :func:`_fit` measures the truth.

    If the file cannot be written (a read-only workspace, a full disk) the rows
    are trimmed anyway and the note says the rest is gone. Returning 108k
    tokens because the disk was full would end the run the budget exists to
    protect.

    The ``sample`` competes for the same characters and is measured in the same
    probe, so it can never push the result over the cap. It yields, one row at a
    time, rather than leaving the caller with a preview and no actual rows: a
    spot-check is worth more than the fourth copy of one label, and nothing at
    all under ``results`` is worth less than either.
    """
    header = {key: value for key, value in out.items() if key not in ("results", "note")}
    written: Path | None = None
    try:
        written = _spill_path(root, run_id)
        written.write_text(
            json.dumps({**header, "results": rows}, default=str, indent=1), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - a failed spill still has to return a result
        logger.warning("could not write the analyze_image table: %s", exc)
        written = None

    if written is not None:
        header["results_file"] = str(written)

    # `shown` is what the sentence says and what the sentence's length depends
    # on. Probe with `shown = len(rows)`, the widest number it can ever be, so
    # the note the caller actually gets is never longer than the one that was
    # measured.
    widest = " ".join([*notes, _spill_sentence(len(rows), len(rows), written, sampled=True)])
    sample = _pick_sample(rows, MAX_SAMPLE_ROWS)
    while True:
        probe = {
            **header,
            **({"sample": sample} if sample else {}),
            "results_shown": len(rows),
            "results_total": len(rows),
            "note": widest,
        }
        shown = _fit(probe, rows, budget)
        if shown or not sample:
            break
        sample = sample[:-1]

    out.clear()
    out.update(header)
    if sample:
        out["sample"] = sample
    out["results"] = rows[:shown]
    out["results_shown"] = shown
    out["results_total"] = len(rows)
    out["note"] = " ".join(
        [*notes, _spill_sentence(len(rows), shown, written, sampled=bool(sample))]
    )
