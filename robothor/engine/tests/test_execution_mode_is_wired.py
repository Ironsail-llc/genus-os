"""An abstraction nobody reads is decoration.

2026-08-27. This session alone produced six controls that were built, tested,
wired into startup logging and completely inert -- and, worse, four guards
written to catch inertness that were themselves inert, because they grepped
source text and matched a comment or a docstring.

So these assert on the AST, never on text. A mode tracker that is never told
what served a request reports CLOUD forever, which is exactly the state the
engine was already in when the fleet spent 29 hours on the local tier.

The parity check matters as much as the presence check: the model breaker
records success on two separate paths (streaming and not), and a signal wired
to only one of them is half-blind in a way that looks fine in production
because the mode does eventually flip -- just not for streaming-only agents.
"""

import ast
import pathlib

import robothor.engine.llm_client as llm_client


def _call_names(tree: ast.AST) -> list[str]:
    """Names of every function actually CALLED — not mentioned in prose."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.append(func.attr)
            elif isinstance(func, ast.Name):
                names.append(func.id)
    return names


def _llm_client_calls() -> list[str]:
    source = pathlib.Path(llm_client.__file__).read_text()
    return _call_names(ast.parse(source))


class TestTheTrackerIsActuallyTold:
    def test_completions_are_recorded_at_all(self):
        assert "_record_execution_mode" in _llm_client_calls(), (
            "the execution-mode tracker is never told what served a request; "
            "it will report CLOUD forever"
        )

    def test_every_success_path_reports_parity_with_the_breaker(self):
        """Streaming and non-streaming both record success; both must record mode."""
        calls = _llm_client_calls()
        successes = calls.count("record_success")
        signals = calls.count("_record_execution_mode")
        assert successes >= 2, "expected the breaker's two success paths"
        assert signals == successes, (
            f"{successes} success paths but {signals} mode signals — "
            "a signal wired to only one path is half-blind"
        )

    def test_the_signal_actually_reaches_the_tracker(self):
        """Parity on a helper proves nothing if the helper is hollow — the exact
        shape of the resume loop that logged progress and never called anything."""
        source = pathlib.Path(llm_client.__file__).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_record_execution_mode":
                assert "record_completion" in _call_names(node), "the helper is hollow"
                return
        raise AssertionError("_record_execution_mode is not defined")


class TestTheSignalIsHarmless:
    def test_recording_an_empty_model_is_ignored(self):
        from robothor.engine.execution_mode import ExecutionModeTracker

        t = ExecutionModeTracker()
        t.record_completion("")
        assert t.snapshot()["last_model"] is None

    def test_a_tracker_failure_can_never_break_an_llm_call(self):
        """Mode is telemetry. It must not be able to fail a request that worked."""
        source = pathlib.Path(llm_client.__file__).read_text()
        tree = ast.parse(source)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if "record_completion" in _call_names(node):
                guarded = True
        assert guarded, "record_completion must be called inside a try/except"
