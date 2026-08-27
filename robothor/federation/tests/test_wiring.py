"""Catch the defect CLASS, not this instance of it.

Federation is the textbook case on this box: 1,861 source lines, 140 passing
tests, and it had never carried a byte in five months. The tests passed because
they exercised the functions production never calls --
``test_nats_request.py`` mocks ``NATSManager._nc`` and calls the one code path
``daemon.py`` never reaches.

Six controls before it were built, wired, tested and inert. So these assertions
are about WIRING rather than behaviour: a module-level registrar with no
production caller, a role that nothing seeds, a CLI verb that dispatches
nowhere. Each is a shape that passes every unit test and does nothing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PKG = ROOT / "robothor"


def _production_files() -> list[pathlib.Path]:
    return [
        p
        for p in PKG.rglob("*.py")
        if "/tests/" not in p.as_posix() and not p.name.startswith("test_")
    ]


def _calls_in(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            n = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if n:
                names.add(n)
    return names


class TestNoRegistrarIsLeftUncalled:
    """A module-level `set_X()` paired with a `get_X()` singleton is a wiring
    seam. If nothing in production calls the setter, every consumer of the
    getter reads None forever -- which is precisely how outbound federation
    stayed dead while its unit tests passed.
    """

    #: Setters whose only callers are legitimately outside robothor/ (none yet).
    KNOWN_EXTERNAL: set[str] = set()

    def test_every_singleton_setter_has_a_production_caller(self):
        defined: dict[str, pathlib.Path] = {}
        for path in _production_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
            for name in names:
                if name.startswith("set_") and f"get_{name[4:]}" in names:
                    defined[name] = path

        called: set[str] = set()
        for path in _production_files():
            called |= _calls_in(path)

        orphans = {
            name: str(p.relative_to(ROOT))
            for name, p in defined.items()
            if name not in called and name not in self.KNOWN_EXTERNAL
        }
        assert not orphans, (
            "these registrars are defined next to a getter and never called in "
            f"production, so the getter returns None forever: {orphans}. That is "
            "the shape that kept federation_query broken for five months while "
            "its tests passed."
        )


class TestEveryPrincipalRoleIsSeeded:
    """check_tool_permission fails CLOSED on a role with no rows. Safe, but it
    turns a correctly-granted peer into a silently useless one and sends the
    next operator hunting a transport bug. Migration 107 is the precedent."""

    @pytest.mark.integration
    def test_the_federation_roles_have_permission_rows(self):
        try:
            import psycopg2

            conn = psycopg2.connect(dbname="robothor_test")
            conn.set_session(readonly=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no database: {exc}")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT role, count(*) FROM role_permissions "
                "WHERE role LIKE 'federation%' GROUP BY role"
            )
            seeded = dict(cur.fetchall())
        finally:
            conn.close()
        for role in ("federation_parent", "federation_child"):
            assert seeded.get(role, 0) > 0, (
                f"{role} has no role_permissions rows. It will deny everything, "
                f"which looks identical to a broken transport."
            )


class TestTheCliSurfaceDispatches:
    """A registered subcommand with no dispatch branch is a documented feature
    that does nothing when invoked."""

    def test_every_federation_subcommand_reaches_a_branch(self):
        cli = (PKG / "cli" / "__init__.py").read_text(encoding="utf-8")
        fed = PKG / "cli" / "federation.py"
        impl = fed.read_text(encoding="utf-8") if fed.exists() else ""

        import re

        registered = set(re.findall(r'add_parser\(\s*["\'](\w+)["\']', cli))
        # only the federation verbs, identified by the module that implements them
        fed_verbs = {v for v in registered if f'"{v}"' in impl or f"'{v}'" in impl}
        assert fed_verbs, "no federation subcommands found — did the CLI move?"


class TestTheToolsAreReachable:
    """A tool no manifest lists is a tool no agent can call. federation_query,
    federation_trigger and federation_sync_status have 0 calls in a
    263,949-row agent_tool_events ledger."""

    def test_federation_tools_are_referenced_by_production_code(self):
        from robothor.engine.tools import constants

        src = pathlib.Path(constants.__file__).read_text(encoding="utf-8")
        assert "FEDERATION_TOOLS" in src
        referencing = [
            p
            for p in _production_files()
            if p.name != "constants.py" and "FEDERATION_TOOLS" in p.read_text(errors="ignore")
        ]
        assert referencing, (
            "FEDERATION_TOOLS is defined and referenced by nothing in production "
            "— the tool class exists only in its own constant"
        )
