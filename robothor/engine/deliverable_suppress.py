"""Is this mention of a path a requirement at all?

A different question from "what shape does it require", and the one the
extractor gets wrong in the more expensive direction. Hostile review 2026-09-16
(C3a): five of five prohibition phrasings produced a `PathItem`, so at
``enforce`` the engine re-asked the agent to write the file the task had
forbidden and then failed the run when it refused.

The asymmetry decides every judgement here. Suppressing a real deliverable
costs a false negative — the control stays quiet on a task it could have helped
— while demanding a forbidden one costs a run, an operator alert, and an agent
pushed toward producing the thing it was told not to produce. Only one of those
is recoverable, so this errs toward silence.

Split out of ``deliverable_extract`` when the size ratchet said so: it is a
cohesive cluster with one entry point and no dependency on the rest of the
extractor.
"""

from __future__ import annotations

import re

#: Chinese prohibition and hypothetical markers, the counterpart of
#: ``_SUPPRESSOR_RE``. Without these the zh anchors would reintroduce exactly
#: the C3a defect in a second language.
_ZH_SUPPRESSOR_RE = re.compile(
    "(?:不要|不得|禁止|请勿|切勿|不能|无需|不需要|如果|若|假如|例如|比如|示例|之前.{0,8}要求)"
)

#: Words that turn "save it to P" into something other than a requirement to
#: save it to P: a prohibition, a hypothetical, a report of what someone else
#: was told, or an illustration.
#:
#: MEASURED by hostile review 2026-09-16 (C3a): five of five prohibition
#: phrasings produced a `PathItem`, so at `enforce` the engine re-asked the
#: agent to write the file the task had forbidden and then failed the run when
#: it refused. Two Safety specs name a path for content an agent should
#: decline. The asymmetry decides the design: suppressing a real deliverable
#: costs a false negative, which is this module's declared safe failure;
#: demanding a forbidden one costs a run, an operator alert, and an agent
#: pushed toward producing the thing it was told not to produce.
_SUPPRESSOR_RE = re.compile(
    r"\b(?:"
    r"do\s+not|don'?t|does\s+not|doesn'?t|never|no\s+circumstances|"
    r"must\s+not|should\s+not|shouldn'?t|cannot|can'?t|won'?t|will\s+not|"
    r"avoid|refrain\s+from|refuse[sd]?|declines?|"
    r"if|unless|when(?:ever)?\s+asked|instead\s+of|rather\s+than|"
    r"was\s+told\s+to|were\s+told\s+to|had\s+been\s+asked|previously\s+asked|"
    # Hypothetical modals only. `can` and `may` are left out on purpose: "you
    # can save it to X" is how half of real instructions are phrased, and
    # suppressing those would disarm the control on ordinary tasks.
    r"could|might|would\s+(?:be|go)|"
    r"for\s+example|for\s+instance|such\s+as|some\s+teams"
    r")\b",
    re.IGNORECASE,
)

#: Where a suppressor stops applying. A clause boundary, because "Do not
#: include markdown. Save the table to results/x.tsv" is a requirement and
#: "Do not save the table to results/x.tsv" is not, and the only thing
#: separating them is the full stop.
#:
#: The optional quote after the terminator is US typographic style, where the
#: full stop goes INSIDE the closing quotation mark. Without it the `never`
#: inside a quoted claim reached forward across the sentence end and suppressed
#: the next sentence's plain output requirement — measured on
#: `06_Safety_Alignment_task_9`, which extracted nothing although its spec ends
#: "Please save the report in /tmp_workspace/post.md." (re-review 2026-09-16,
#: R4). It failed closed, so it cost coverage rather than correctness — and it
#: cost half the evidence for the refusal audit, since a spec with no contract
#: can have no unsatisfied verdict to record.
_CLAUSE_BREAK_RE = re.compile(r"""[.;:!?]['"\u2019\u201d]?\s|\n|[。；！？]['"\u2019\u201d]?""")

#: "e.g." / "i.e." — an illustration whose own full stop would otherwise be
#: read as the end of the clause that introduced it.
_ABBREVIATION_RE = re.compile(r"\b[ei]\.?\s?[ge]\.", re.IGNORECASE)

#: How close the abbreviation has to sit to count. It introduces the very next
#: clause, not the paragraph.
_ABBREVIATION_REACH = 40

#: How far back a suppressor can reach inside its own clause. Long enough for
#: "Under no circumstances should you write the credentials to …", short
#: enough that a prohibition two sentences up cannot disarm a real requirement.
_SUPPRESSOR_REACH = 160


def is_suppressed(text: str, index: int) -> bool:
    """Does a prohibition, hypothetical or illustration govern this match?

    Looks back to the nearest clause boundary — never across one — within a
    bounded window.
    """
    whole = text[max(0, index - _SUPPRESSOR_REACH) : index]
    # `e.g.` and `i.e.` carry a full stop that is not a clause break, so they
    # have to be read BEFORE the window is trimmed — trimming at their own
    # period is what let "e.g. save the output to results/x.json" through.
    if _ABBREVIATION_RE.search(whole[-_ABBREVIATION_REACH:]):
        return True
    window = whole
    breaks = list(_CLAUSE_BREAK_RE.finditer(window))
    if breaks:
        window = window[breaks[-1].end() :]
    if _SUPPRESSOR_RE.search(window) is not None:
        return True
    return _ZH_SUPPRESSOR_RE.search(window) is not None
