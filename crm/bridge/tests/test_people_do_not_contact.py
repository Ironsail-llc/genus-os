"""PATCH /api/people/{id} must be able to set (and clear) the opt-out flag.

Without an API path the column is operator-invisible: the only way to honour
an unsubscribe request would be raw SQL. The DAL is mocked — this asserts the
route maps `doNotContact` to the DAL's `do_not_contact` kwarg and passes the
tenant through, exactly like every other field on this endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_patch_sets_do_not_contact(test_client):
    with patch("routers.people.update_person", return_value=True) as mock:
        r = await test_client.patch(f"/api/people/{PID}", json={"doNotContact": True})

    assert r.status_code == 200
    assert r.json() == {"success": True, "id": PID}
    assert mock.call_args.kwargs["do_not_contact"] is True


@pytest.mark.asyncio
async def test_patch_clears_do_not_contact(test_client):
    """False must reach the DAL — a truthiness filter here would make the
    opt-out one-way, which is worse than not having it."""
    with patch("routers.people.update_person", return_value=True) as mock:
        r = await test_client.patch(f"/api/people/{PID}", json={"doNotContact": False})

    assert r.status_code == 200
    assert mock.call_args.kwargs["do_not_contact"] is False


@pytest.mark.asyncio
async def test_patch_omitting_the_field_does_not_touch_it(test_client):
    with patch("routers.people.update_person", return_value=True) as mock:
        await test_client.patch(f"/api/people/{PID}", json={"city": "Springfield"})

    assert "do_not_contact" not in mock.call_args.kwargs
