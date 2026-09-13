"""Ask the syntax tree what a function calls, instead of grepping its source.

``inspect.getsource(module)`` plus ``assert "warm_channels" in src`` is the
shape three wiring guards here were written in, and it passes on a mention in a
docstring, a name in a comment, a line inside ``if False:``, and an import that
nothing calls. ``test_plugin_groups_are_consumed.py`` already rejects greps as
evidence for exactly this reason, and ``test_delivery_by_channel.py`` carries
the AST version inline — this is that pattern, shared, so the next wiring guard
does not reach for a substring because it was the shorter thing to write.

Nothing here proves the call *runs*. It proves the call is written in the
function it is supposed to be written in, which is the difference between a
regression that fails CI and one that ships.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

__all__ = [
    "called_names",
    "function_def",
    "keywords_of_call",
    "module_tree",
    "string_constants",
]


def module_tree(dotted: str) -> ast.Module:
    """The parsed source of an importable module."""
    module = importlib.import_module(dotted)
    assert module.__file__ is not None, f"{dotted} has no source file"
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def function_def(dotted: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The (possibly async) function ``name`` defined in module ``dotted``.

    Matches a method as readily as a module-level function: the walk is over
    every definition in the file, so ``SlackBot.start`` is found as ``start``.
    """
    for node in ast.walk(module_tree(dotted)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{dotted} defines no function named {name!r}")


def called_names(node: ast.AST) -> set[str]:
    """Every name called anywhere inside ``node``.

    A plain call contributes its ``id``; an attribute call contributes the
    attribute (``asyncio.create_task`` → ``create_task``), which is what a
    wiring assertion is actually about.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            found = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
            if found:
                names.add(found)
    return names


def string_constants(node: ast.AST) -> set[str]:
    """Every string literal inside ``node``, minus its own docstring.

    The docstring is excluded because that is precisely where the substring
    guards this module replaces used to find their evidence: the prose
    explaining why a call matters is not the call.
    """
    body = list(getattr(node, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    found: set[str] = set()
    for statement in body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                found.add(child.value)
    return found


def keywords_of_call(node: ast.AST, call: str) -> set[str]:
    """The keyword argument names passed to ``call`` inside ``node``.

    ``**kwargs`` contributes nothing (its key is ``None``), which is correct: a
    guard asserting an argument was passed explicitly must not be satisfied by
    a splat that may or may not contain it.
    """
    keywords: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
        if name != call:
            continue
        keywords.update(keyword.arg for keyword in child.keywords if keyword.arg)
    return keywords
