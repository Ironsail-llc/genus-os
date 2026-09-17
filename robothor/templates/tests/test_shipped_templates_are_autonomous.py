"""Nothing the platform ships may arrive with an approval gate switched on.

2026-09-17. `crm-steward`'s shipped manifest template carried
``v2.guardrails: [human_approval]`` and ``human_approval_tools:
[delete_person]``. Every instance installing that template inherited a nightly,
unattended hygiene scan that stopped at each duplicate contact to ask a person,
waited out ``human_approval_timeout`` and was then denied — so the run deleted
nothing and the operator got one Telegram prompt per duplicate.

Genus OS runs agents autonomously by design. The approval gate is a feature an
instance may opt into, per agent, for the tools it chooses; a template that
ships it turns an opt-in into a default for everyone who installs it. The gate
therefore lives in these files only as a COMMENTED example.

A grep, not a schema rule: the manifest schema must keep accepting the keys —
they are supported configuration — and what this test pins is that the shipped
files do not USE them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]

#: Every manifest the platform ships, plus the schema doc whose examples an
#: operator copies. `docs/agents/*.yaml` is instance data and gitignored, so
#: `schema.yaml` is the one file under it that belongs to the platform.
_SHIPPED = sorted(_REPO.glob("templates/**/*.yaml")) + [_REPO / "docs" / "agents" / "schema.yaml"]

#: The sentence a commented example must carry, so a reader copying it out
#: knows what they are turning on.
_OPT_IN_SENTENCE = "opt-in; the default is autonomous"


def _is_field_definition(value: object) -> bool:
    """``{type: list[string], default: [], description: ...}`` — `schema.yaml`
    DESCRIBING the key, not a manifest setting it. Documenting supported
    configuration is the point of that file; what this test forbids is a
    shipped file switching the gate on."""
    return isinstance(value, dict) and "type" in value and "default" in value


def _active_approval_keys(document: object, trail: str = "") -> set[str]:
    """Paths of every `human_approval*` key with a value that DOES something.

    An empty list or a false flag is the schema stating its default, not a
    template arming a gate.
    """
    found: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            path = f"{trail}.{key}" if trail else str(key)
            if str(key).startswith("human_approval") and value and not _is_field_definition(value):
                found.add(path)
            found |= _active_approval_keys(value, path)
    elif isinstance(document, list):
        for index, item in enumerate(document):
            found |= _active_approval_keys(item, f"{trail}[{index}]")
        if trail.endswith("guardrails") and "human_approval" in document:
            found.add(f"{trail}: human_approval")
    return found


def _load(path: Path) -> object:
    """Jinja stripped, the way `genus init` renders it, so the YAML parses.

    An unparseable file FAILS, naming itself. Returning ``None`` here would
    have made this gate pass on exactly the file it could not read — a shipped
    manifest that arms the gate and happens to break the Jinja stripper would
    be waved through, which is the "a check that sees nothing reports nothing"
    failure this suite exists to prevent.
    """
    text = re.sub(r"\{\%.*?\%\}", "", path.read_text(), flags=re.DOTALL)
    text = re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", "placeholder", text)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        pytest.fail(f"{path.relative_to(_REPO)} does not parse after Jinja stripping: {exc}")


@pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_shipped_file_arms_an_approval_gate(path: Path) -> None:
    active = sorted(_active_approval_keys(_load(path)))

    assert not active, (
        f"{path.relative_to(_REPO)} ships an armed approval gate at {active}. "
        "The platform runs agents autonomously; move it to a commented example "
        f"carrying '{_OPT_IN_SENTENCE}'."
    )


def test_the_commented_examples_say_the_gate_is_opt_in() -> None:
    """A commented-out key with no explanation is an invitation to uncomment
    it. Wherever a shipped file still SHOWS the gate, it says what it is."""
    offenders: list[str] = []
    for path in _SHIPPED:
        if not path.exists():
            continue
        text = path.read_text()
        commented = [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("#") and "human_approval" in line
        ]
        if commented and _OPT_IN_SENTENCE not in text:
            offenders.append(str(path.relative_to(_REPO)))

    assert not offenders, (
        f"{offenders} show the approval gate without saying it is optional; "
        f"add the sentence '{_OPT_IN_SENTENCE}'."
    )


def test_the_grep_would_catch_a_regression() -> None:
    """A gate test that passes because its scanner sees nothing is the defect
    it exists to prevent."""
    armed = {
        "v2": {
            "guardrails": ["write_path_restrict", "human_approval"],
            "human_approval_tools": ["delete_person"],
        }
    }

    assert _active_approval_keys(armed) == {
        "v2.human_approval_tools",
        "v2.guardrails: human_approval",
    }
    assert _active_approval_keys({"v2": {"human_approval_tools": [], "guardrails": []}}) == set()
    assert (
        _active_approval_keys(
            {"optional": {"human_approval_tools": {"type": "list[string]", "default": []}}}
        )
        == set()
    ), "a schema DESCRIBING the key is not a manifest setting it"


# ── the docs may not re-grow the nudge ────────────────────────────────

#: Every page that describes the approval gate. A claim about the gate that
#: contradicts the platform lives in one of these two.
_APPROVAL_DOCS = (
    _REPO / "docs" / "TOOLS.md",
    _REPO / "docs" / "runbooks" / "approval-enforce.md",
)

#: Ids of the agents the platform ships, from the template tree itself rather
#: than a hand-maintained list — a doc claim about `crm-steward` has to be
#: caught whatever the template is called next year.
_SHIPPED_AGENT_IDS = frozenset(
    path.parent.name for path in _REPO.glob("templates/agents/*/*/manifest.template.yaml")
)

#: Present tense. A doc may RECORD that a template once armed the gate (the
#: 2026-09-17 note does exactly that, in the past tense); what it may not do is
#: tell today's reader that one does.
_ASSERTS_NOW = re.compile(
    r"\b(?:declares|sets|arms|enables|ships with|comes with)\b", re.IGNORECASE
)

#: Tool families that are NOT sanctioned gate targets. The gate is for an
#: irreversible EXTERNAL action that moves money or cannot be taken back — a
#: refund, a payment. Ordinary work stays automated, and that includes every
#: name here, all five of which the runbook once listed as "candidates" for
#: the next gate.
_UNSANCTIONED_GATE_TARGETS = (
    "outbound email",
    "email/sms",
    "sms",
    "`exec`",
    "calendar write",
    "destructive crm",
    "git_push",
    "write_file",
    "gws_gmail_send",
)

#: Words that turn a mention into a proposal.
_CANDIDATE_CUE = re.compile(
    r"\bcandidates?\b|\bnext tool\b|\btools to gate\b|\bgate next\b", re.IGNORECASE
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if part.strip()]


@pytest.mark.parametrize("path", _APPROVAL_DOCS, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_doc_claims_a_shipped_template_arms_the_gate(path: Path) -> None:
    """`approval-enforce.md` carried "crm-steward NOW DECLARES
    v2.guardrails: [human_approval]" for a day after the template stopped doing
    so. A runbook that describes an instance the platform no longer ships sends
    the next reader to copy it."""
    offenders = [
        sentence
        for sentence in _sentences(path.read_text())
        if "human_approval" in sentence
        and _ASSERTS_NOW.search(sentence)
        and any(agent_id in sentence for agent_id in _SHIPPED_AGENT_IDS)
    ]

    assert not offenders, (
        f"{path.relative_to(_REPO)} says a shipped template arms the approval "
        f"gate, in the present tense: {offenders}. None does — the platform "
        "runs agents autonomously. Record it in the past tense or drop it."
    )


@pytest.mark.parametrize("path", _APPROVAL_DOCS, ids=lambda p: str(p.relative_to(_REPO)))
def test_no_doc_nominates_the_next_tools_to_gate(path: Path) -> None:
    """The runbook used to end "before adding the next tool to a
    human_approval_tools list (candidates: outbound email/SMS, `exec`,
    payments, calendar writes, destructive CRM mutations)" — a roadmap for
    gating ordinary work, on a platform whose default is autonomy.

    The rule needs a LIST, not a mention: two or more unsanctioned families in
    one sentence carrying a candidate cue. Prose that says such a list does not
    belong here names none of them, and a page must stay able to say "`exec` is
    not gated by this".
    """
    offenders = []
    for sentence in _sentences(path.read_text()):
        if not _CANDIDATE_CUE.search(sentence):
            continue
        named = [t for t in _UNSANCTIONED_GATE_TARGETS if t in sentence.lower()]
        if len(named) >= 2:
            offenders.append((sentence, named))

    assert not offenders, (
        f"{path.relative_to(_REPO)} nominates tools to gate next: {offenders}. "
        "The sanctioned examples are irreversible external actions that move "
        "money — a refund, a payment. Ordinary work stays automated."
    )


@pytest.mark.parametrize("path", _APPROVAL_DOCS, ids=lambda p: str(p.relative_to(_REPO)))
def test_every_approval_doc_opens_with_autonomy_first(path: Path) -> None:
    """The positive half: a forbidden-phrase test passes on an empty file, so
    the section that states the default has to be required, not merely
    un-contradicted."""
    text = path.read_text()

    assert "Autonomy first" in text, path.relative_to(_REPO)
    assert "human_approval_tools: [issue_refund]" in text, (
        f"{path.relative_to(_REPO)} describes the gate without showing how to "
        "turn it on; enabling it must stay easy"
    )


def test_the_doc_scanners_would_catch_the_text_that_was_removed() -> None:
    """Both rules, against the exact prose this branch deleted. A doc gate that
    passes because its regex matches nothing is the defect it exists to
    prevent."""
    claim = (
        "Superseding the 2026-07-13 note below: `crm-steward` now declares "
        "`v2.guardrails: [human_approval]` and `v2.human_approval_tools: "
        "[delete_person]`."
    )
    nudge = (
        "Before adding the next tool to a `human_approval_tools` list (candidates: "
        "outbound email/SMS, `exec`, payments, calendar writes on external "
        "attendees, destructive CRM mutations), verify one real escalation."
    )

    assert _ASSERTS_NOW.search(claim) and any(a in claim for a in _SHIPPED_AGENT_IDS)
    assert _CANDIDATE_CUE.search(nudge)
    assert len([t for t in _UNSANCTIONED_GATE_TARGETS if t in nudge.lower()]) >= 2

    kept = "This runbook does not nominate candidates; ordinary work belongs automated."
    assert len([t for t in _UNSANCTIONED_GATE_TARGETS if t in kept.lower()]) == 0
    past = "a 2026-09-16 note that said `crm-steward` declared `v2.human_approval_tools`"
    assert not _ASSERTS_NOW.search(past), "the past tense must stay sayable"
