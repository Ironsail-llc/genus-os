"""A test run must never page the operator.

2026-08-27: running the suite sent three real Telegram alerts to Philip's
phone, including "2 CORRUPT offsite (bytes differ from source):
robothor_memory-20260712.sql.gz" -- a fixture filename that reads exactly
like a genuine data-integrity emergency. Another named a pytest tmpdir
outright.

``tests/test_backup_offsite.py`` subprocess-runs the real
``scripts/backup-offsite.sh``, which calls the real
``scripts/send_failure_alert.sh``. The test passes a CLEAN env, so no pytest
marker reaches the subprocess -- but the pager re-sources credentials from
tmpfs itself, so it delivered anyway.

``model_breaker`` already guards this class in Python (``_in_pytest()``,
added after 92 of 145 production escalation rows turned out to be pytest
fixture models). The shell path had no equivalent.

Guard the path every caller crosses, not the one today's caller uses.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PAGER = REPO / "scripts" / "send_failure_alert.sh"


def test_the_pager_refuses_alerts_that_name_a_test_path():
    src = PAGER.read_text()
    assert "pytest-of-" in src, (
        "send_failure_alert.sh has no guard against paging on a message that "
        "names a pytest temp directory, so any suite run can spam the operator "
        "with fixture failures that read like real emergencies"
    )


def test_the_backup_test_uses_the_stub_api_seam():
    """The script exposes ROBOTHOR_TELEGRAM_API_BASE for exactly this."""
    src = (REPO / "tests" / "test_backup_offsite.py").read_text()
    assert "ROBOTHOR_TELEGRAM_API_BASE" in src, (
        "the offsite test drives the real alert path without redirecting it; "
        "the script already supports a stub base URL"
    )


def pager_script_names() -> tuple[str, ...]:
    """The scripts that reach the sender, derived — never hand-maintained.

    The list used to be four names typed into this file, and it was already
    two short: ``thermal-guard.sh`` and ``boot-guard.sh`` both call the sender
    and were invisible to the scan. A hand-maintained list of what a mechanism
    covers drifts from what the mechanism actually is (2026-08-22: three
    separate "hardcoded names" defects turned out to be one), so ask the tree:
    every scripts/*.sh that mentions the sender, plus the sender itself.
    """
    names = {PAGER.name}
    for script in sorted((REPO / "scripts").glob("*.sh")):
        if PAGER.name in script.read_text(errors="ignore"):
            names.add(script.name)
    return tuple(sorted(names))


PAGER_SCRIPTS = "|".join(re.escape(name) for name in pager_script_names())

# What a test that can reach the sender must pin. All three are real, shared,
# durable paths on a live box:
#
#   ROBOTHOR_ALERT_SPOOL_DIR   /var/lib/robothor/alert-spool — a page left here
#       is DELIVERED by the next 5-minute liveness drain. Not a suppressed
#       page: an actual message on the operator's phone, minutes after the
#       suite that wrote it exited.
#   ROBOTHOR_ALERT_STATE_DIR   /run/robothor/alert-cooldown — a stamp here
#       suppresses a REAL page for an hour.
#   ROBOTHOR_ALERT_FALLBACK_STATE_DIR  /tmp/robothor-alert-cooldown-<uid> —
#       where the cooldown lands when the primary is not writable, which is
#       every cron-driven page on this box. Same consequence, different path.
REQUIRED_PINS = (
    "ROBOTHOR_ALERT_SPOOL_DIR",
    "ROBOTHOR_ALERT_STATE_DIR",
    "ROBOTHOR_ALERT_FALLBACK_STATE_DIR",
)


# subprocess entry points that take env=. Declared here (rather than lower,
# where _EnvPinAudit uses it too) because argv-invocation detection needs it
# as well, and a second hand-typed copy is how the two drift apart.
_SUBPROCESS_CALLS = ("run", "Popen", "check_output", "check_call", "call")


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _subprocess_argv_nodes(tree: ast.Module) -> list[ast.expr]:
    """The argv/command expression of every subprocess-launching call in a
    parsed file — ``subprocess.run(THIS, ...)``, ``Popen(THIS, ...)``, or the
    ``args=`` keyword."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) not in _SUBPROCESS_CALLS:
            continue
        argv = node.args[0] if node.args else None
        if argv is None:
            argv = next((kw.value for kw in node.keywords if kw.arg == "args"), None)
        if argv is not None:
            out.append(argv)
    return out


def _literal_strings(node: ast.expr | None, tree: ast.Module, seen: frozenset = frozenset()) -> set[str]:
    """Every string literal an expression can possibly evaluate to.

    Not full symbolic execution — good enough to see through the shapes this
    repo's tests actually use: ``str(SCRIPT)`` / ``str(self.SCRIPT)`` where
    ``SCRIPT`` is a module- or class-level ``REPO_ROOT / "scripts" / "x.sh"``,
    an f-string, or a local ``argv = [...]`` referenced by name at the call
    site. Unresolvable pieces (an unannotated function parameter, a dynamic
    join) contribute nothing rather than raising — this only ever needs to
    prove a script name IS reachable, never that one is absent.
    """
    if node is None:
        return set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.JoinedStr):
        out: set[str] = set()
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.add(value.value)
            elif isinstance(value, ast.FormattedValue):
                out |= _literal_strings(value.value, tree, seen)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _literal_strings(node.left, tree, seen) | _literal_strings(node.right, tree, seen)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = set()
        for elt in node.elts:
            out |= _literal_strings(elt, tree, seen)
        return out
    if isinstance(node, ast.Starred):
        return _literal_strings(node.value, tree, seen)
    if isinstance(node, ast.Call):
        out = set()
        for arg in node.args:
            out |= _literal_strings(arg, tree, seen)
        return out
    if isinstance(node, ast.Name):
        key = ("name", node.id)
        if key in seen:
            return set()
        seen = seen | {key}
        out = set()
        for assign in ast.walk(tree):
            if isinstance(assign, ast.Assign):
                for target in assign.targets:
                    if isinstance(target, ast.Name) and target.id == node.id:
                        out |= _literal_strings(assign.value, tree, seen)
        return out
    if isinstance(node, ast.Attribute):
        key = ("attr", node.attr)
        if key in seen:
            return set()
        seen = seen | {key}
        out = set()
        for assign in ast.walk(tree):
            if isinstance(assign, ast.Assign):
                for target in assign.targets:
                    # self.SCRIPT = ... (instance attribute) or a class-body
                    # SCRIPT = ... (accessed elsewhere as self.SCRIPT).
                    if isinstance(target, ast.Attribute) and target.attr == node.attr:
                        out |= _literal_strings(assign.value, tree, seen)
                    if isinstance(target, ast.Name) and target.id == node.attr:
                        out |= _literal_strings(assign.value, tree, seen)
        return out
    return set()


def invokes_a_pager_script(text: str) -> bool:
    """True only when a real ``subprocess``/``Popen`` call's argv names one of
    the pager scripts — never a comment, a docstring, an assertion on a
    script's own stdout, or fixture data (a crontab template, an installed
    file's basename) that merely happens to contain the name.

    ``test_gen_cron_map.py`` docs the ``$W`` cron-wrapper convention in a
    CRONTAB fixture string and never runs cron-wrapper.sh; ``test_instance_
    doctor.py`` asserts the doctor's stdout NAMES a drifted
    ``robothor-thermal-guard.sh`` file and never runs it. A whole-file
    substring scan cannot tell either apart from a test that actually drives
    the script — see test_the_scan_ignores_a_script_name_mentioned_only_in_data
    below.
    """
    if "subprocess" not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for argv in _subprocess_argv_nodes(tree):
        if any(re.search(PAGER_SCRIPTS, s) for s in _literal_strings(argv, tree)):
            return True
    return False


def tests_that_can_reach_the_pager() -> list[tuple[Path, str]]:
    """Test files that RUN something able to page — not ones that merely read
    a script's source (tests/test_cold_boot.py asserts on the sender's text
    and executes nothing), and not ones that merely NAME a pager script in a
    docstring, a comment, fixture data, or an assertion on some other
    script's output."""
    found = []
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        text = path.read_text(errors="ignore")
        if "subprocess" not in text:
            continue
        if not invokes_a_pager_script(text) and "ROBOTHOR_TELEGRAM_BOT_TOKEN" not in text:
            continue
        found.append((path, text))
    return found


_MENTION_ONLY_FIXTURE = '''
import subprocess

# A crontab fixture that documents the $W convention — this is DATA, not an
# invocation. The real script this test drives is "echo".
CRONTAB = """
W=/srv/genus/scripts/cron-wrapper.sh
0 4 * * * $W scripts/present.sh
"""


def test_reads_a_crontab_fixture():
    result = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert result.stdout.strip() == "hello"
    assert "cron-wrapper.sh" in CRONTAB
'''

_REAL_INVOCATION_VIA_VARIABLE_FIXTURE = '''
import subprocess
from pathlib import Path

REPO_ROOT = Path("/repo")
SCRIPT = REPO_ROOT / "scripts" / "send_failure_alert.sh"


def run(unit):
    argv = ["bash", str(SCRIPT), unit]
    return subprocess.run(argv, capture_output=True, text=True)
'''


def test_the_scan_ignores_a_script_name_mentioned_only_in_data_or_prose():
    """The shape that made test_gen_cron_map.py and test_instance_doctor.py
    false positives: a pager script's name inside a crontab fixture string or
    an assertion on another script's stdout, never passed to subprocess."""
    assert not invokes_a_pager_script(_MENTION_ONLY_FIXTURE), (
        "a pager script name inside fixture data or a docstring must not "
        "count as a real invocation"
    )


def test_the_scan_still_finds_a_real_invocation_reached_through_a_variable():
    """The shape every real offender actually uses: a module-level SCRIPT
    (or self.SCRIPT) built from REPO_ROOT / "scripts" / "<name>.sh", passed to
    subprocess via str(SCRIPT) inside a local argv list. Narrowing the scan
    must not blind it to this — the ordinary way these tests drive a script."""
    assert invokes_a_pager_script(_REAL_INVOCATION_VIA_VARIABLE_FIXTURE), (
        "a subprocess call whose argv names a pager script through a "
        "variable must still be treated as reaching the pager"
    )


def test_every_test_that_can_page_pins_the_spool_and_the_state_dirs():
    """Redirecting the API is not enough any more.

    ROBOTHOR_TELEGRAM_API_BASE stops the send, but an undeliverable page is
    no longer dropped — it is SPOOLED, and the real spool is drained every 5
    minutes by root's liveness tick. So a test that neutralises delivery and
    forgets the spool does not avoid paging the operator; it delays it. The
    2026-08-27 accident with a longer fuse.
    """
    offenders = {}
    for path, text in tests_that_can_reach_the_pager():
        missing = [pin for pin in REQUIRED_PINS if pin not in text]
        if missing:
            offenders[path.name] = missing
    assert not offenders, (
        "these tests can run the pager without pinning the durable state it "
        f"writes — a page spooled by the suite is delivered for real by the "
        f"next liveness drain: {offenders}"
    )


def test_the_ratchet_actually_finds_the_files_it_is_meant_to_guard():
    """A ratchet whose scan matches nothing passes forever. Name the files."""
    names = {p.name for p, _ in tests_that_can_reach_the_pager()}
    for expected in (
        "test_pager_hardening.py",
        "test_failure_alerts.py",
        "test_liveness_watchdog.py",
        "test_backup_offsite.py",
    ):
        assert expected in names, f"the scan no longer sees {expected}"


def test_no_test_invokes_the_pager_against_the_real_api():
    """Any test that runs a script which can page must neutralise the send."""
    offenders = []
    for path in (REPO / "tests").rglob("test_*.py"):
        text = path.read_text(errors="ignore")
        if not invokes_a_pager_script(text):
            continue
        neutralised = (
            "ROBOTHOR_TELEGRAM_API_BASE" in text
            or "ROBOTHOR_ALERT_SUPPRESS" in text
            or "ROBOTHOR_TELEGRAM_BOT_TOKEN" in text
        )
        if not neutralised:
            offenders.append(path.name)
    assert not offenders, (
        f"these tests drive a script that can page the operator without "
        f"redirecting or suppressing delivery: {sorted(offenders)}"
    )


def test_the_derived_script_list_covers_every_caller_of_the_sender():
    """The two the hand-written list missed, named explicitly.

    A derivation that silently starts matching nothing passes forever, so
    assert on the callers that were invisible before it existed — a future
    test of thermal-guard.sh or boot-guard.sh is now inside the ratchet on
    the day it is written, not on the day someone remembers this file.
    """
    names = pager_script_names()
    for expected in (
        "send_failure_alert.sh",
        "liveness_probe.sh",
        "cron-wrapper.sh",
        "backup-offsite.sh",
        "thermal-guard.sh",
    ):
        assert expected in names, f"{expected} calls the sender but is not in the scan"


# ── Per-call-site pinning ────────────────────────────────────────────────────
#
# The file-level check above answers "does this FILE mention the pins", which
# is exactly the question that let today's 14:40 page through:
# test_missing_remote_config_fails_loudly built its own env= dict inline,
# among siblings that went through a pinned helper, so every pin appeared in
# the file and none of them reached that subprocess. The file was clean and
# the operator's phone rang.
#
# So the pins are checked where they are actually applied: per env= dict, per
# subprocess call.
REQUIRED_CALL_PINS = REQUIRED_PINS + (
    # /run/robothor/secrets.env is real and readable on a live box: with no
    # override the sender recovers the operator's ACTUAL Telegram credentials
    # and delivers, whatever the API base says.
    "ROBOTHOR_SECRETS_FILE",
    # And with credentials in hand, this is what keeps the POST off
    # api.telegram.org.
    "ROBOTHOR_TELEGRAM_API_BASE",
)

class _EnvPinAudit:
    """Resolve the ``env=`` expression of every subprocess call in one file.

    Accepts the shapes a test legitimately uses: a dict literal carrying the
    pins itself, a helper whose own literal carries them (``base_env(...)``,
    ``run_send(...)``), and a merge of one of those (``dict(base, ...)``,
    ``{**base, ...}``). Rejects an env= dict that pins nothing and inherits
    nothing — the shape that pages.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(errors="ignore"))
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        # NAME = {...} shared between tests: module constants and class
        # attributes ONLY. A local of the same name inside some other test is
        # not what this call site inherits — reading one as if it were made a
        # fully pinned helper look unpinned, which is how a ratchet trains
        # people to widen it.
        self.dict_names: dict[str, ast.Dict] = {}
        # A function is a pinning helper if any single dict literal in its
        # body carries every pin — that is the dict its callers inherit.
        self.helpers: dict[str, set[str]] = {}
        self.functions: list[ast.FunctionDef] = []
        shared: list[ast.stmt] = list(self.tree.body)
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                shared.extend(node.body)
        for node in shared:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.dict_names[target.id] = node.value
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(node)
        self.helpers = {func.name: set() for func in self.functions}
        # A helper can be built out of another helper (``env = dict(BASE)``,
        # ``return base_env(...)``), so settle it by iteration rather than in
        # one pass — a single pass blesses only the helper that spells the
        # dict out itself, and reports every caller of the wrapper as an
        # offender.
        required = set(REQUIRED_CALL_PINS)
        for _ in range(len(self.functions) + 1):
            changed = False
            for func in self.functions:
                best = self.helpers[func.name]
                candidates: list[set[str]] = []
                for node in ast.walk(func):
                    if isinstance(node, ast.Dict):
                        candidates.append(self._dict_keys(node, set()))
                    elif isinstance(node, ast.Return) and node.value is not None:
                        candidates.append(self._resolve(node.value, set()) or set())
                for keys in candidates:
                    if len(keys & required) > len(best & required):
                        best, changed = keys, True
                self.helpers[func.name] = best
            if not changed:
                break

    # ── resolution ───────────────────────────────────────────────────────────

    def _dict_keys(self, node: ast.Dict, seen: set[str]) -> set[str]:
        keys: set[str] = set()
        for key, value in zip(node.keys, node.values):
            if key is None:  # {**other, ...}
                keys |= self._resolve(value, seen) or set()
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
        return keys

    def _resolve(self, node: ast.expr | None, seen: set[str]) -> set[str] | None:
        """Env keys the expression is known to carry; None = cannot tell."""
        if node is None:
            return None
        if isinstance(node, ast.Dict):
            return self._dict_keys(node, seen)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            # A local binding wins over a module constant of the same name —
            # that is what Python does, and `env` is a name every test uses.
            local = self._resolve_local(node, seen)
            if local is not None:
                return local
            if node.id in self.dict_names:
                return self._dict_keys(self.dict_names[node.id], seen | {node.id})
            return None
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name == "dict":
                keys: set[str] = set()
                for arg in node.args:
                    keys |= self._resolve(arg, seen) or set()
                for kw in node.keywords:
                    if kw.arg is None:
                        keys |= self._resolve(kw.value, seen) or set()
                    else:
                        keys.add(kw.arg)
                return keys
            if name in self.helpers:
                return set(self.helpers[name])
            return None
        return None

    def _enclosing(self, node: ast.AST) -> ast.FunctionDef | None:
        cur = self.parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = self.parents.get(cur)
        return None

    def _params(self, func: ast.FunctionDef) -> list[str]:
        args = func.args
        return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]

    def _resolve_local(self, name_node: ast.Name, seen: set[str]) -> set[str] | None:
        """A local variable: every assignment to it must carry the pins."""
        func = self._enclosing(name_node)
        if func is None:
            return None
        keys: set[str] | None = None
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name_node.id for t in node.targets
            ):
                got = self._resolve(node.value, seen | {name_node.id})
                if got is None:
                    return None
                keys = got if keys is None else (keys & got)
        return keys

    # ── the audit ────────────────────────────────────────────────────────────

    def offenders(self) -> list[str]:
        """``file:line`` for every subprocess call whose env= misses a pin."""
        out: list[str] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node.func) not in _SUBPROCESS_CALLS:
                continue
            env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
            if env is None:
                # No env= at all: the child inherits the suite's own
                # environment, which carries no ROBOTHOR_* pins to lose.
                continue
            out.extend(self._check(env, node))
        return sorted(set(out))

    def _check(self, env: ast.expr, call: ast.Call) -> list[str]:
        missing_at = []
        # env=<a parameter of this helper>: the pins have to be at the call
        # sites instead — run_send(tmp_path, env) is only as safe as its
        # callers.
        func = self._enclosing(env)
        if (
            isinstance(env, ast.Name)
            and func is not None
            and env.id in self._params(func)
        ):
            index = self._params(func).index(env.id)
            for site in ast.walk(self.tree):
                if not isinstance(site, ast.Call):
                    continue
                if _call_name(site.func) != func.name:
                    continue
                passed: ast.expr | None = None
                if len(site.args) > index:
                    passed = site.args[index]
                for kw in site.keywords:
                    if kw.arg == env.id:
                        passed = kw.value
                if passed is None:
                    continue
                keys = self._resolve(passed, set())
                missing = [p for p in REQUIRED_CALL_PINS if p not in (keys or set())]
                if missing:
                    missing_at.append(f"{self.path.name}:{passed.lineno} {missing}")
            return missing_at
        keys = self._resolve(env, set())
        missing = [p for p in REQUIRED_CALL_PINS if p not in (keys or set())]
        if missing:
            missing_at.append(f"{self.path.name}:{env.lineno} {missing}")
        return missing_at


def env_pin_offenders(path: Path) -> list[str]:
    return _EnvPinAudit(path).offenders()


_FIXTURE = '''
import subprocess

BASE = {
    "ROBOTHOR_ALERT_SPOOL_DIR": "/tmp/fixture/spool",
    "ROBOTHOR_ALERT_STATE_DIR": "/tmp/fixture/state",
    "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": "/tmp/fixture/fallback",
    "ROBOTHOR_SECRETS_FILE": "/tmp/fixture/no-such-secrets.env",
    "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
}


def helper(**extra):
    env = dict(BASE)
    env.update(extra)
    return env


def run_via_helper():
    subprocess.run(["bash", "send_failure_alert.sh", "u"], env=helper(HOME="/tmp"))


def run_via_splat():
    subprocess.run(["bash", "send_failure_alert.sh", "u"], env={**BASE, "HOME": "/tmp"})


def run_via_local():
    env = helper()
    subprocess.run(["bash", "send_failure_alert.sh", "u"], env=env)


def run_with_its_own_dict():
    subprocess.run(
        ["bash", "send_failure_alert.sh", "u"],
        env={
            "HOME": "/tmp",
            "ROBOTHOR_ALERT_SUPPRESS": "1",
            "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
        },
    )
'''


def test_the_scan_finds_an_unpinned_call_site_among_pinned_siblings(tmp_path: Path):
    """The shape that paged the operator at 14:40 today.

    Three call sites inherit the pins; the fourth builds its own env= dict
    with only ROBOTHOR_TELEGRAM_API_BASE in it. Every required pin appears
    somewhere in the file, so a substring check over the file text sees
    nothing wrong.
    """
    fixture = tmp_path / "test_fixture_unpinned.py"
    fixture.write_text(_FIXTURE)

    offenders = env_pin_offenders(fixture)

    assert len(offenders) == 1, (
        f"expected exactly the one unpinned call site, got {offenders}"
    )
    bad_line = int(offenders[0].split(":")[1].split()[0])
    lines = _FIXTURE.splitlines()
    start = next(i for i, l in enumerate(lines, 1) if "run_with_its_own_dict" in l)
    assert bad_line >= start, (
        f"the offender was reported at line {bad_line}, outside the unpinned "
        f"function that starts at {start}"
    )
    assert "ROBOTHOR_ALERT_SPOOL_DIR" in offenders[0], (
        "the report must name the pins that are missing, not just the line"
    )


def test_every_subprocess_call_in_a_pager_reaching_test_pins_the_sender_seams():
    """Per call site, not per file.

    A page spooled by the suite is delivered for real by root's next 5-minute
    liveness drain, and the sender re-sources credentials from tmpfs itself,
    so a subprocess that inherits none of the pins pages the operator however
    clean the rest of the file is.
    """
    offenders: list[str] = []
    for path, _text in tests_that_can_reach_the_pager():
        offenders.extend(env_pin_offenders(path))
    assert not offenders, (
        "these subprocess call sites run a script that can page without "
        "pinning the sender's durable state — each is one fixture failure "
        "away from the operator's phone:\n  " + "\n  ".join(offenders)
    )


# ── The Python senders ───────────────────────────────────────────────────────
#
# Everything above audits the SHELL path. It is not the only way a test can
# reach the operator's phone.
#
# scripts/guardrail_watch.py carries its own sender: send_telegram() POSTs to a
# hardcoded https://api.telegram.org, reading ROBOTHOR_TELEGRAM_BOT_TOKEN and
# ROBOTHOR_TELEGRAM_CHAT_ID straight out of os.environ. There is no API-base
# seam to redirect, no spool to pin and no cooldown to stamp — none of the
# pins above apply to it at all. A test that drives main() on a box where the
# operator's credentials are exported does not spool a page for later; it
# delivers one, now.
#
# Two independent stops, because the one that is easy to forget is the one
# that pages: the credentials are removed from every test's environment
# (root conftest.py), and the sender itself refuses to deliver from inside a
# test run — the same guard the shell pager already has.


def _guardrail_watch():
    """Load scripts/guardrail_watch.py as a module, the way its own tests do."""
    spec = importlib.util.spec_from_file_location(
        "guardrail_watch_under_ratchet", REPO / "scripts" / "guardrail_watch.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIVE_TELEGRAM_ENV = ("ROBOTHOR_TELEGRAM_BOT_TOKEN", "ROBOTHOR_TELEGRAM_CHAT_ID")


def test_no_test_runs_with_live_telegram_credentials_in_its_environment():
    """The operator's shell exports these; a bare `pytest` inherits them."""
    present = [key for key in LIVE_TELEGRAM_ENV if key in os.environ]
    assert not present, (
        f"{present} reached a test — any Python sender reading os.environ can "
        "now deliver a fixture message to the operator"
    )


def test_the_root_conftest_strips_the_credentials():
    import conftest

    monkeypatch = pytest.MonkeyPatch()
    try:
        for key in LIVE_TELEGRAM_ENV:
            monkeypatch.setenv(key, "fixture-value-not-a-credential")
        conftest.strip_live_telegram_credentials(monkeypatch)
        still_set = [key for key in LIVE_TELEGRAM_ENV if key in os.environ]
        assert not still_set, f"the strip left {still_set} in place"
    finally:
        monkeypatch.undo()


def test_the_credential_strip_applies_to_every_test_without_being_requested(request):
    """autouse, or it only protects the tests that remembered to ask.

    This test never asks for the strip; ``request.fixturenames`` lists it only
    if pytest applied it on its own.
    """
    assert "_no_live_telegram_credentials" in request.fixturenames, (
        "the Telegram credential strip is not autouse, so it guards exactly "
        "the tests that already knew about the hazard"
    )


def test_the_python_sender_refuses_to_deliver_from_inside_a_test(monkeypatch):
    """Mirror of the shell pager's own in-test guard.

    Credentials can arrive by routes the conftest strip does not see — a
    monkeypatched env, a .env a fixture sources, a future caller that passes
    them in — so the sender says no on its own account too.
    """
    gw = _guardrail_watch()
    calls: list[object] = []
    monkeypatch.setattr(gw.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    for key in LIVE_TELEGRAM_ENV:
        monkeypatch.setenv(key, "fixture-value-not-a-credential")

    assert gw.send_telegram("fixture nag — must never leave the box") is False
    assert calls == [], (
        "guardrail_watch.send_telegram POSTed from inside a test run; on a box "
        "with real credentials in scope that is a fixture message on the "
        "operator's phone, with no spool, cooldown or API seam in the way"
    )


# ── The guardrail-watch stub helpers ─────────────────────────────────────────
#
# Four test files define a helper of the same name, `_stub_sibling_checks`,
# with the same stated job: default every check main() calls to a safe pass so
# that driving main() does not reach the live box or the operator. Four copies
# of one contract drift (2026-08-22: three "hardcoded names" defects turned out
# to be one), and the copy in test_guardrail_watch_slos.py had already lost
# send_telegram — so its main() tests ran the real sender.

_STUB_HELPER = "_stub_sibling_checks"


def _monkeypatched_attrs(func: ast.FunctionDef) -> set[str]:
    """The attribute names a helper hands to ``monkeypatch.setattr(gw, "x", …)``."""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "setattr":
            continue
        if len(node.args) < 2:
            continue
        target = node.args[1]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            names.add(target.value)
    return names


def stub_helpers() -> dict[Path, set[str]]:
    """Every test file defining ``_stub_sibling_checks``, and what it stubs."""
    out: dict[Path, set[str]] = {}
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == _STUB_HELPER:
                out[path] = _monkeypatched_attrs(node)
    return out


def test_the_stub_helper_scan_finds_the_files_that_define_one():
    """A scan that matches nothing passes forever. Name the files."""
    names = {path.name for path in stub_helpers()}
    for expected in (
        "test_guardrail_watch_ordering.py",
        "test_guardrail_watch_slos.py",
        "test_instance_doctor.py",
        "test_flag_audit.py",
    ):
        assert expected in names, f"the scan no longer sees {expected}"


def test_every_stub_sibling_checks_helper_neutralises_the_python_sender():
    offenders = sorted(
        path.name for path, attrs in stub_helpers().items() if "send_telegram" not in attrs
    )
    assert not offenders, (
        "these _stub_sibling_checks helpers claim to default every live-box "
        "check to a safe pass but leave guardrail_watch.send_telegram real, "
        f"so a nag raised by main() is delivered for real: {offenders}"
    )
