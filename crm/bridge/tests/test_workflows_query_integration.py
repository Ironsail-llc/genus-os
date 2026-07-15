import pytest

pytestmark = pytest.mark.integration

from contextlib import contextmanager

from routers import workflows


def test_workflow_queries_valid(db_conn, monkeypatch):
    @contextmanager
    def _fake():
        yield db_conn

    monkeypatch.setattr(workflows, "get_connection", _fake)
    assert isinstance(workflows._list_workflows(), list)
    assert isinstance(workflows._workflow_runs("none", 5), list)
