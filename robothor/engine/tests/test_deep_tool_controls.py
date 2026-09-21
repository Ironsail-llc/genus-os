"""Deep tools retain their admission owner and consult durable controls per call."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from robothor.engine import rlm_tool
from robothor.engine.runtime import controls
from robothor.engine.runtime.deep_admission import execute_deep_checked
from robothor.engine.runtime.provider_budget import DurableStopError
from robothor.engine.tests.test_runtime_controls import runtime_db  # noqa: F401
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.mark.parametrize("check", ["context", "stop", "store_unavailable"])
def test_deep_callbacks_keep_owner_and_deny_later_dispatch(runtime_db, monkeypatch, check):  # noqa: F811
    run = SimpleNamespace(id=str(uuid4()), tenant_id="deep-tools")
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs(id,tenant_id) VALUES (%s,%s)", (run.id, run.tenant_id))
    owner = ContextVar("test_deep_owner", default="missing")
    invoked, captured = [], {}

    def underlying():
        invoked.append(owner.get())
        return "Synthetic result"

    for factory in [
        "_make_search_memory_fn",
        "_make_get_entity_fn",
        "_make_read_file_fn",
        "_make_memory_block_read_fn",
        "_make_web_search_fn",
        "_make_exec_fn",
    ]:
        monkeypatch.setattr(rlm_tool, factory, lambda *args, **kwargs: underlying)

    def worker(**kwargs):
        captured.update(rlm_tool._build_custom_tools("/synthetic"))
        return {"response": "Prepared"}

    token = owner.set("owner")
    try:
        with patch.object(rlm_tool, "execute_deep_reason", side_effect=worker):
            execute_deep_checked(run=run, workspace="/synthetic", query="Synthetic analysis")
    finally:
        owner.reset(token)
    callbacks = [definition["tool"] for definition in captured.values()]
    assert len(callbacks) == 6
    if check in {"stop", "store_unavailable"}:
        if check == "stop":
            controls.issue(run.tenant_id, run.id, "cancel")
        else:

            def unavailable(*args):
                raise ConnectionError("Control store unavailable")

            monkeypatch.setattr(controls, "stopped", unavailable)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(callback) for callback in callbacks]
            for future in futures:
                with pytest.raises(DurableStopError if check == "stop" else ConnectionError):
                    future.result()
        assert not invoked
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert (
                list(pool.map(lambda callback: callback(), callbacks)) == ["Synthetic result"] * 6
            )
        assert invoked == ["owner"] * 6


def test_failed_deep_worker_does_not_leak_its_owner(monkeypatch):
    from robothor.engine.runtime.deep_tools import owner

    previous = owner.get()
    monkeypatch.setattr(controls, "stopped", lambda *args: False)
    with patch.object(rlm_tool, "execute_deep_reason", side_effect=RuntimeError("Worker failed")):
        with pytest.raises(RuntimeError, match="Worker failed"):
            execute_deep_checked(
                run=SimpleNamespace(tenant_id="tenant", id="run"),
                workspace="/synthetic",
                query="Synthetic",
            )
    assert owner.get() is previous
