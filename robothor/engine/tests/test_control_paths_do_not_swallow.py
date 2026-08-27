"""A control that fails silently is worse than one that is absent.

Every inert control found today shared a mechanism: something went wrong and
nothing said so. The most literal form of that is `except Exception: pass`, and
the engine has 27 of them. Most are harmless — a best-effort sd_notify, a
cosmetic lookup — but a few sit on the path that TELLS THE OPERATOR SOMETHING
IS WRONG.

The worst was daemon.py's watchdog: it pages "PostgreSQL unreachable for 3
consecutive checks" and swallowed any failure to send. So a database outage and
a failed page produced exactly the same observable state — nothing. This repo
has been here before ("alerts.py delivered its first message ever"; "the pager
read HTTP 401 as delivered").

S110 is enabled for the engine so new ones cannot appear. Files that still hold
non-control-path instances carry an explicit per-file ignore rather than a
blanket suppression, so the remaining work is visible instead of erased.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]

#: Modules whose job is to notice or report trouble. A silent swallow here
#: destroys the only evidence that anything happened.
_CONTROL_PATHS = ("daemon.py", "alerts.py", "provider_alerts.py", "detectors.py")


def _silent_swallows(path: Path) -> list[int]:
    """Line numbers of `except ...: pass` with no logging in the handler."""
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr | ast.Pass)]
        logged = any(
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and "log" in ast.dump(n.value.func).lower()
            for n in node.body
        )
        if not body and not logged:
            out.append(node.lineno)
    return out


class TestNothingOnAControlPathFailsSilently:
    def test_the_watchdog_alert_path_reports_its_own_failure(self):
        """A page that fails must not look identical to no problem at all."""
        offenders = _silent_swallows(_ENGINE / "daemon.py")
        assert not offenders, (
            f"daemon.py swallows exceptions with no log at line(s) {offenders} — "
            "a failed operator page is indistinguishable from a healthy system"
        )

    def test_no_control_module_swallows_silently(self):
        offenders = {
            name: lines
            for name in _CONTROL_PATHS
            if (lines := _silent_swallows(_ENGINE / name))
        }
        assert not offenders, f"silent swallows on control paths: {offenders}"
