"""Breadth as a primitive, not as ninety hand-written extension points.

Measured from DeepSeek Harness's own source on 27 August: **143 distinct
`ctx.*` surfaces**, and `ctx.provide()` — the generic service-registration
call — used 330 times. Their architecture doc says "~9 kinds"; the code says
service registration is itself the primitive, and everything else is built
on it. That is why "everything is a plugin" holds for them and why counting
our curated points against their doc was measuring the wrong thing.

Nine named groups cannot catch that by becoming ten. The shape has to
change: one point through which a package registers an arbitrary named
service, and anything — core or another package — looks it up.

What does NOT change is the safety that the named groups already carry, and
which dsh's architecture doc addresses for none of its surfaces: a service
declaring a reserved name is refused rather than silently winning, the
contract version is negotiated, and a broken package is reported instead of
taking the process down. Their breadth, our containment.
"""

from __future__ import annotations

import pytest

from robothor.plugins import reload_plugins


class _SvcEP:
    group = "genus.services"

    def __init__(self, services: dict, name: str = "testsvc"):
        self.name = name
        self._services = services

    def load(self):
        return {"genus_contract_version": "1.0", "services": self._services}


@pytest.fixture
def install(monkeypatch):
    def _install(services: dict):
        from robothor.plugins import loader

        monkeypatch.setattr(loader, "_discover", lambda: [_SvcEP(services)])
        reload_plugins()

    yield _install
    from robothor.plugins import loader

    monkeypatch.setattr(loader, "_discover", list)
    reload_plugins()


class TestTheGroupExists:
    def test_the_loader_declares_it(self):
        from robothor.plugins.loader import _GROUPS

        assert _GROUPS.get("genus.services") == "services"

    def test_the_pluginset_carries_it(self, install):
        from robothor.plugins import load_plugins

        install({"vector_store": object()})
        assert "vector_store" in (load_plugins(reserved_names=set()).services or {})


class TestAnyNamedServiceResolves:
    def test_a_registered_service_is_retrievable(self, install):
        from robothor.engine.services import get_service

        sentinel = object()
        install({"vector_store": sentinel})
        assert get_service("vector_store") is sentinel

    def test_an_unregistered_name_is_none_not_an_error(self):
        from robothor.engine.services import get_service

        assert get_service("nothing_provides_this") is None

    def test_services_are_listable(self, install):
        from robothor.engine.services import list_services

        install({"a": object(), "b": object()})
        assert {"a", "b"} <= set(list_services())

    def test_arbitrary_shapes_are_allowed(self, install):
        """The point is that the platform does not pre-declare the kinds."""
        from robothor.engine.services import get_service

        install({"a_callable": lambda: 1, "a_dict": {"k": "v"}, "an_int": 7})
        assert get_service("a_callable")() == 1
        assert get_service("a_dict")["k"] == "v"
        assert get_service("an_int") == 7


class TestContainmentSurvivesTheBreadth:
    def test_a_reserved_name_is_refused(self, install):
        """dsh documents no version or name negotiation; this is the trade."""
        from robothor.engine.services import get_service, reserved_service_names

        reserved = next(iter(reserved_service_names()))
        install({reserved: object()})
        assert get_service(reserved) is None, f"a package claimed the reserved {reserved!r}"

    def test_the_refusal_is_reported_not_silent(self):
        from robothor.engine.services import reserved_service_names
        from robothor.plugins import load_plugins

        reserved = next(iter(reserved_service_names()))
        ep = _SvcEP({reserved: object()})
        loaded = load_plugins(entry_points=[ep], reserved_names=set(reserved_service_names()))
        assert any(reserved in f.reason for f in loaded.failures), loaded.failures

    def test_a_wrong_contract_version_is_refused(self):
        from robothor.plugins import load_plugins

        class _Old:
            name, group = "old", "genus.services"

            def load(self):
                return {"genus_contract_version": "0.9", "services": {"x": object()}}

        loaded = load_plugins(entry_points=[_Old()], reserved_names=set())
        assert not (loaded.services or {})
        assert loaded.failures

    def test_a_broken_package_does_not_break_lookups(self, monkeypatch):
        from robothor.engine.services import get_service
        from robothor.plugins import loader

        class _Boom:
            name, group = "boom", "genus.services"

            def load(self):
                raise RuntimeError("bad package")

        monkeypatch.setattr(loader, "_discover", lambda: [_Boom()])
        reload_plugins()
        assert get_service("anything") is None  # must not raise


class TestItTracksReloads:
    def test_a_service_installed_at_runtime_resolves(self, install):
        from robothor.engine.services import get_service

        assert get_service("late_arrival") is None
        install({"late_arrival": "here"})
        assert get_service("late_arrival") == "here"

    def test_an_uninstalled_service_disappears(self, install, monkeypatch):
        from robothor.engine.services import get_service
        from robothor.plugins import loader

        install({"temporary": "x"})
        assert get_service("temporary") == "x"
        monkeypatch.setattr(loader, "_discover", list)
        reload_plugins()
        assert get_service("temporary") is None


class TestItIsReachableFromATool:
    def test_the_tool_context_exposes_lookup(self):
        """A registry no handler can reach is a dict."""
        from robothor.engine.tools.dispatch import ToolContext

        assert hasattr(ToolContext, "get_service"), (
            "ToolContext has no get_service — plugin tools cannot reach services"
        )


class TestRefusalIsAllOrNothing:
    """One reserved name costs a package all of its services.

    Observed with a real installed package: it declared `vector_store` and
    `memory` together, and the loader refused the whole entry point, so the
    legitimate service was lost with the takeover attempt.

    That is the loader's existing behaviour for every group, and it is the
    right way round — a package reaching for `memory` has shown what it is
    willing to do, and honouring the rest of its registrations rewards
    exactly the wrong instinct. Pinned so it is a decision rather than a
    surprise, since the failure is silent from the caller's side: the
    service simply is not there.
    """

    def test_a_takeover_attempt_costs_the_whole_package(self, install):
        from robothor.engine.services import get_service

        install({"legitimate": "kept?", "memory": "TAKEOVER"})
        assert get_service("memory") is None
        assert get_service("legitimate") is None, (
            "the package kept a service despite attempting a reserved name"
        )

    def test_a_clean_package_keeps_everything(self, install):
        from robothor.engine.services import get_service

        install({"one": 1, "two": 2})
        assert get_service("one") == 1 and get_service("two") == 2


class TestToolContextCanActuallyReachAService:
    """`genus.services` had no consumer, which made it a registry not a seam.

    `ToolContext.get_service` was written as "the counterpart to ctx.get in
    harnesses built on a service kernel" — and nothing in the engine, in a
    built-in handler, or even in this file ever called it. The tests reached
    `robothor.engine.services.get_service` directly, so the group's ONLY
    consumer path was never exercised. Nine wired groups beat ten with one
    inert.
    """

    def test_a_handler_reaches_a_registered_service_through_its_context(self, install):
        """Through ToolContext, the path a handler would actually use — not
        robothor.engine.services.get_service, which is what every other test
        here calls and is why the consumer path stayed unexercised."""
        from robothor.engine.tools.dispatch import ToolContext

        install({"answer": lambda: 42})
        svc = ToolContext(agent_id="probe").get_service("answer")
        assert svc is not None, "ToolContext cannot reach a registered service"
        assert svc() == 42

    def test_an_unregistered_service_is_none_not_an_error(self, install):
        from robothor.engine.tools.dispatch import ToolContext

        install({"answer": lambda: 42})
        assert ToolContext(agent_id="probe").get_service("nothing_provides_this") is None
