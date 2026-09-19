"""Admission rules for bounded native sales research manifests."""

import json

from robothor.operations.store import Conflict
from robothor.sales.models import Dossier
from robothor.sales.research_fanout import ResearchFanout

READ_TOOLS = {
    "web_search",
    "web_fetch",
    "web_render",
    "sales_get_prospect",
    "sales_get_context",
    "write_file",
}
FANOUT_TOOLS = {"sales_research_parallel", "write_file"}


def validate_tools(config, allowed):
    if not config.tools_allowed or not set(config.tools_allowed) <= allowed:
        raise Conflict("Sales research requires a bounded read/research manifest")
    if "write_file" in config.tools_allowed and (
        not config.write_path_allowlist
        or "write_path_restrict" not in config.guardrails
        or config.guardrails_opt_out
    ):
        raise Conflict("Sales status writes require an enforced path allowlist")


def prepare_research(config, snapshot, stage, tenant_id, message):
    """Check both reviewed manifests before admitting any paid native work."""
    if stage == "analyst":
        if config.can_spawn_agents or config.service_role != "sales_analyst":
            raise Conflict("Sales analysis requires a non-spawning analyst manifest")
        validate_tools(config, {"sales_get_report", "write_file"})
        return None, message
    if not config.can_spawn_agents:
        validate_tools(config, READ_TOOLS)
        return None, message
    validate_tools(config, FANOUT_TOOLS)
    if (
        stage != "research"
        or snapshot is None
        or not config.fleet_release_id
        or config.service_role != "sales_research_agent"
        or "sales_research_parallel" not in config.tools_allowed
        or len(config.spawn_allowed_agents) != 1
        or config.max_spawn_total != 3
        or config.max_spawn_batch != 3
        or config.max_nesting_depth != 1
    ):
        raise Conflict("Research delegation requires a pinned, three-child, single-depth manifest")
    try:
        child = snapshot.agent(config.spawn_allowed_agents[0])
    except (ValueError, KeyError):
        raise Conflict("Research worker is missing from the selected release") from None
    validate_tools(child, READ_TOOLS)
    retrieval_tools = [t for t in ("web_fetch", "web_render") if t in child.tools_allowed]
    if not retrieval_tools:
        raise Conflict("Research worker must have a page retrieval tool")
    if (
        child.can_spawn_agents
        or child.continuous
        or child.auto_task
        or child.downstream_agents
        or child.service_role != "sales_agent"
        or str(child.delivery_mode) != "none"
        or not child.hard_budget
        or not 0 < child.max_cost_usd <= 1
        or not 0 < child.timeout_seconds <= 180
        or not 0 < child.safety_cap <= 20
    ):
        raise Conflict("Research worker must be a bounded non-spawning read-only worker")
    try:
        request = json.loads(message)
        if request["output_schema"] != Dossier.model_json_schema():
            raise ValueError
        context = request["untrusted_business_data"]
        if not isinstance(context, dict) or not isinstance(context.get("prospect"), dict):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise Conflict("Delegated research requires the native dossier request") from None
    fanout = ResearchFanout(config.id, child.id, tenant_id, context)
    fanout.first_read_tool = retrieval_tools[0]
    request["delegation"] = {
        "tool": "sales_research_parallel",
        "arguments": "buying_case only",
        "instruction": "Choose one active approved buying case and call sales_research_parallel once. Genus supplies all company context and three fixed topics. Return the resulting dossier JSON; do not add facts or claims. If no approved case is suitable, explain the missing policy and do not delegate.",
    }
    return fanout, json.dumps(request, default=str)
