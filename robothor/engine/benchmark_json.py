"""Bounded deterministic assertions over an agent's exact JSON deliverable."""

import json
import re
from typing import Any

_MISSING = object()
_TYPES = {"object", "array", "string", "boolean", "integer", "number", "null"}


def validate_assertions(checks: Any) -> str | None:
    if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
        return "json_assertions must contain 1–100 checks"
    for check in checks:
        if not isinstance(check, dict):
            return "json_assertions entries must be mappings"
        op = check.get("op")
        keys = {"path", "op"} if op == "absent" else {"path", "op", "value"}
        if "optional" in check:
            if type(check["optional"]) is not bool or op == "absent":
                return "json_assertions optional must be a boolean on a value check"
            keys.add("optional")
        if (
            set(check) != keys
            or not isinstance(op, str)
            or op not in {"equals", "absent", "contains", "length", "type"}
        ):
            return "json_assertions require a supported op, path and exact value fields"
        path = check["path"]
        if (
            not isinstance(path, str)
            or len(path) > 1000
            or (path and not path.startswith("/"))
            or re.search(r"~(?![01])", path)
        ):
            return "json_assertions path must be a JSON pointer (empty means root)"
        value = check.get("value")
        if op == "type" and (not isinstance(value, str) or value not in _TYPES):
            return "json_assertions type must name a JSON type"
        if op == "length" and (type(value) is not int or value < 0):
            return "json_assertions length must be a nonnegative integer"
    try:
        if len(json.dumps(checks, allow_nan=False)) > 128_000:
            return "json_assertions exceeds the size limit"
    except (TypeError, ValueError, RecursionError):
        return "json_assertions values must be finite JSON data"
    return None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _nonfinite(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _resolve(root: Any, path: str) -> Any:
    value = root
    for token in path.split("/")[1:] if path else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(token, _MISSING)
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
    return value


def _equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return bool(left == right)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _matches(value: Any, check: dict[str, Any]) -> bool:
    op, wanted = check["op"], check.get("value")
    if op == "absent":
        return value is _MISSING
    if value is _MISSING:
        return bool(check.get("optional", False))
    if op == "equals":
        return _equal(value, wanted)
    if op == "length":
        return isinstance(value, (str, list, dict)) and len(value) == wanted
    if op == "contains":
        return isinstance(value, list) and any(_equal(item, wanted) for item in value)
    types = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "null": value is None,
    }
    # `wanted` is a validated type name here: validate_assertions refuses any
    # `op: "type"` check whose value is not one of _TYPES.
    return types[str(wanted)]


def grade_json(output: Any, checks: Any) -> dict[str, Any]:
    """Return diagnostics without copying source data into the grading record."""
    error = validate_assertions(checks)
    if error:
        return {"passed": False, "error": error}
    if not isinstance(output, str) or len(output) > 128_000:
        return {"passed": False, "error": "JSON output exceeds the size limit"}
    try:
        root = json.loads(output, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        # Also reject numeric overflow such as 1e999, which json.loads maps to inf.
        json.dumps(root, allow_nan=False)
        failed = [
            i
            for i, check in enumerate(checks)
            if not _matches(_resolve(root, check["path"]), check)
        ]
    except (ValueError, TypeError, RecursionError):
        return {"passed": False, "error": "Output is not one strict JSON document"}
    return {"passed": not failed, "failed_checks": failed}
