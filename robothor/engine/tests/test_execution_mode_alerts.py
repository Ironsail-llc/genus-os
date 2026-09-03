"""One page that names the consequence, not fifty that name the mechanism.

2026-08-27. The fleet ran 29 hours on the local tier and the operator was never
told -- they found out because agents felt slow. The opposite failure is just as
bad: once admission starts deferring, fifty deferred ticks must not become fifty
pushes. Deferrals belong in agent_guardrail_events, where they can be counted;
the operator gets ONE message when the economics change.

``provider_alerts`` already records what a good page says: a unit name is not a
consequence. "OpenRouter exhausted" makes the operator go find out what it
means for them. "Now serving locally; background work paced, your Telegram
turns unaffected" does not.

And never from a test: 92 of 145 production escalation rows were once pytest
fixture models.
"""

import robothor.engine.execution_mode_alerts as ema


class TestItNeverPagesFromATestSession:
    def test_entry_is_suppressed_under_pytest(self, monkeypatch):
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        ema.alert_mode_entered("local", background_deferred=3)
        assert sent == [], "a test session paged the operator"

    def test_exit_is_suppressed_under_pytest(self, monkeypatch):
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        ema.alert_mode_left("local", duration_seconds=3600, catch_up_count=4)
        assert sent == []


class TestThePageNamesTheConsequence:
    def test_entry_says_what_it_means_for_the_operator(self, monkeypatch):
        monkeypatch.setattr(ema, "_in_pytest", lambda: False)
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        ema.alert_mode_entered("local", background_deferred=3)
        body = " ".join(sent[0]).lower()
        assert "local" in body
        assert "interactive" in body, "the page must say what still works"

    def test_exit_reports_the_duration_and_the_catch_up(self, monkeypatch):
        monkeypatch.setattr(ema, "_in_pytest", lambda: False)
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        ema.alert_mode_left("local", duration_seconds=7200, catch_up_count=4)
        body = " ".join(sent[0])
        assert "2h" in body or "120" in body, f"duration not reported: {body}"
        assert "4" in body


class TestBurstsCollapse:
    def test_a_repeated_entry_pages_once(self, monkeypatch):
        monkeypatch.setattr(ema, "_in_pytest", lambda: False)
        ema.reset_for_test()
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        for _ in range(50):
            ema.alert_mode_entered("local", background_deferred=1)
        assert len(sent) == 1, f"50 entries produced {len(sent)} pages"

    def test_leaving_re_arms_the_entry_page(self, monkeypatch):
        """A second, genuinely new outage must still reach the operator."""
        monkeypatch.setattr(ema, "_in_pytest", lambda: False)
        ema.reset_for_test()
        sent = []
        monkeypatch.setattr(ema, "_deliver", lambda *a: sent.append(a))
        ema.alert_mode_entered("local", background_deferred=1)
        ema.alert_mode_left("local", duration_seconds=60, catch_up_count=0)
        ema.alert_mode_entered("local", background_deferred=1)
        assert len(sent) == 3


class TestItCannotBreakTheEngine:
    def test_a_failing_delivery_never_raises(self, monkeypatch):
        monkeypatch.setattr(ema, "_in_pytest", lambda: False)
        ema.reset_for_test()

        def boom(*a):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(ema, "_deliver", boom)
        ema.alert_mode_entered("local", background_deferred=1)  # must not raise


class TestTheAlertsAreActuallyWired:
    def test_both_transitions_page(self):
        """A page wired to nothing is the failure this campaign keeps finding."""
        import ast
        import pathlib

        import robothor.engine.execution_mode as em

        tree = ast.parse(pathlib.Path(em.__file__).read_text())
        called = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert "_page_entered" in called, "entering a mode pages nobody"
        assert "_page_left" in called, "leaving a mode pages nobody"

    def test_the_helpers_actually_reach_the_alert_module(self):
        """Parity on a helper proves nothing if the helper is hollow."""
        import ast
        import pathlib

        import robothor.engine.execution_mode as em

        tree = ast.parse(pathlib.Path(em.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_page_entered", "_page_left"):
                inner = {
                    getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                }
                assert inner & {"alert_mode_entered", "alert_mode_left"}, f"{node.name} is hollow"
