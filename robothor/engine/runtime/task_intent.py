"""Conservative host recognition of a complete task-creation command.

Only quoted title/body commands are recognized. Other natural language continues
through the ordinary model loop; a model's finalReport flag cannot widen scope.
"""

import json
import re

_QUOTED = r'"(?:[^"\\]|\\.)*"'
_COMMAND = re.compile(
    r"\s*(?:please\s+)?(?:create|add)\s+(?:exactly\s+)?(?:one|a)\s+task\s+"
    r"(?:with\s+title|titled|called|named)\s+(?P<title>" + _QUOTED + r")"
    r"(?:\s+(?:and|with)\s+(?:body|description)\s+(?P<body>" + _QUOTED + r"))?"
    r"(?P<tail>.*)\Z",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINTS = {"use the task tool", "do not contact anyone", "don't contact anyone"}


def standalone(text, args):
    if not isinstance(text, str) or len(text) > 10_000 or args.get("status", "TODO") != "TODO":
        return False
    match = _COMMAND.fullmatch(text)
    if match is None:
        return False
    # Only optional task-tool/no-contact constraints can follow the quoted
    # fields. An additional answer, action, format or contextual reference is
    # not a recognized standalone command, even when the model says it is.
    clauses = [part.strip().lower() for part in re.split(r"[.;]", match["tail"]) if part.strip()]
    if any(part not in _CONSTRAINTS for part in clauses):
        return False
    try:
        title = json.loads(match["title"])
        body = json.loads(match["body"]) if match["body"] is not None else ""
    except (TypeError, ValueError):
        return False
    return title == args.get("title") and body == (args.get("body") or "")


def permits(session, args):
    from robothor.engine.skill_contract import loaded_skill_text
    from robothor.engine.task_context import read_context

    context = read_context(session.messages)
    if loaded_skill_text(session) or (context and context.get("steering")):
        return False
    text = getattr(session, "originating_message", None) or session.run.task_text
    return standalone(text, args)
