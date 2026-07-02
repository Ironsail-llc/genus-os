"""Workflow expressions use a safe AST evaluator, not eval() (Wave-2, W2-13).

The old eval(expr, {"__builtins__": {}}, ...) still allowed attribute-chain
escapes (value.__class__.__bases__...). _safe_eval whitelists AST nodes, blocks
dunder access, and permits only whitelisted helper functions.
"""

from __future__ import annotations

import pytest

from robothor.engine.workflow import _eval_condition, _render_template, _safe_eval


class TestSafeEval:
    def test_literals_and_arithmetic(self):
        assert _safe_eval("1 + 2 * 3", {}) == 7

    def test_context_names_and_attributes(self):
        ctx = {"run": {"status": "ok", "count": 3}}
        assert _safe_eval("run.status == 'ok'", ctx) is True
        assert _safe_eval("run.count > 2", ctx) is True

    def test_boolean_and_comparison(self):
        assert _safe_eval("value > 0 and value < 10", {"value": 5}) is True
        assert _safe_eval("value in [1, 2, 3]", {"value": 2}) is True

    def test_whitelisted_function(self):
        assert _safe_eval("len(items) == 2", {"items": [1, 2]}) is True

    def test_dunder_attribute_blocked(self):
        with pytest.raises(ValueError, match="private attribute"):
            _safe_eval("value.__class__", {"value": "x"})

    def test_arbitrary_call_blocked(self):
        with pytest.raises(ValueError):
            _safe_eval("open('/etc/passwd')", {})

    def test_import_or_unknown_name_blocked(self):
        with pytest.raises(ValueError, match="unknown name"):
            _safe_eval("__import__", {})


class TestIntegrationPoints:
    def test_render_template(self):
        assert (
            _render_template("status={{ run.status }}", {"run": {"status": "done"}})
            == "status=done"
        )

    def test_render_template_bad_expr_keeps_literal(self):
        # An invalid/blocked expression leaves the {{...}} literal, never raises.
        assert "{{" in _render_template("{{ value.__class__ }}", {"value": "x"})

    def test_eval_condition(self):
        assert _eval_condition("value == 'done'", "done") is True
        assert _eval_condition("value == 'done'", "nope") is False
