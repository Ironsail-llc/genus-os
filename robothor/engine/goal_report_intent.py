"""Recognize requests whose whole answer can be a single factual goal report."""

import json
import re

_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_REFERENCE = rf"(?:this goal|the goal|my goal|that goal|that work|goal (?P<goal>{_UUID}))"
_STATUS = re.compile(
    rf"(?:(?:report|show)(?: me)? (?:the )?(?:progress|status)(?: for| of)?"
    rf"|(?:what is|what's) (?:the )?(?:status|progress) of) {_REFERENCE}"
)
_CONTROL = re.compile(rf"(?P<action>pause|resume|cancel) {_REFERENCE}")
_STATUS_QUESTIONS = {
    "what's finished, and what's still left",
    "what is finished, and what is still left",
    "what's finished and what's still left",
    "what remains to finish this goal",
    "what is the status of this goal",
    "what's the status",
    "what is the status",
    "show goal progress",
    "report goal progress",
    "tell me whether automatic pursuit is enabled",
    "is automatic pursuit enabled",
    "how is this goal doing",
    "how is this goal going",
}
_STATUSES = {
    "pause": ("paused",),
    "resume": ("queued", "running"),
    "cancel": ("canceled",),
}
_GOAL_TOOLS = {
    "get_pursuit_goal",
    "list_pursuit_goals",
    "update_pursuit_goal",
    "report_pursuit_goal",
}


def _parse(text, *, supplemental=False):
    if not isinstance(text, str) or len(text) > 1000:
        return None
    text = " ".join(text.strip().lower().replace("’", "'").split()).rstrip(".!?")
    text = re.sub(r"^please ", "", text)
    if supplemental:
        text = re.sub(r"^also ", "", text)
    if text in _STATUS_QUESTIONS:
        return "status", None
    match = _STATUS.fullmatch(text)
    if match:
        return "status", match.groupdict().get("goal")
    match = _CONTROL.fullmatch(text)
    if match:
        return match["action"], match["goal"]
    return None


def admission(req):
    from robothor.engine.skill_contract import loaded_skill_text
    from robothor.engine.task_context import read_context

    session, run = req.session, req.session.run
    if (
        run.resume_from_run_id
        or str(run.trigger_detail or "").startswith("plan")
        or loaded_skill_text(session)
        or any(
            str(step.step_type) == "tool_call" and step.tool_name not in _GOAL_TOOLS
            for step in run.steps
        )
    ):
        return False, None
    intent = _parse(getattr(session, "originating_message", None) or run.task_text)
    if intent is None:
        return False, None
    action, goal_id = intent
    context = read_context(session.messages)
    for steering in (context or {}).get("steering", []):
        extra = _parse(steering, supplemental=True)
        if extra != ("status", None):
            return False, None
    todos = getattr(session, "todo_list", None)
    if todos and any(item.status != "completed" for item in todos.items):
        return False, None
    if goal_id or action != "status":
        try:
            args = json.loads(req.assistant_msg.tool_calls[0].function.arguments)
            selected = args.get("goal_id", "").lower()
            if goal_id and selected != goal_id:
                return False, None
        except (AttributeError, IndexError, TypeError, ValueError):
            return False, None
        if action != "status" and not any(
            step.tool_name == "update_pursuit_goal"
            and not step.error_message
            and (step.tool_input or {}).get("action") == action
            and (step.tool_input or {}).get("goal_id") == selected
            and (step.tool_output or {}).get("goal", {}).get("id") == selected
            and not (step.tool_output or {}).get("error")
            for step in run.steps
        ):
            return False, None
    return True, _STATUSES.get(action)
