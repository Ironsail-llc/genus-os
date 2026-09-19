"""Contact import reads only the authenticated person's existing record."""

import json
from unittest.mock import MagicMock

import pytest

from robothor.autonomy.models import Scope
from robothor.autonomy.onboarding import import_contact_profile


def test_contact_import_is_scoped_and_returns_only_a_resource_reference():
    store = MagicMock()
    cur = store.transaction.return_value.__enter__.return_value
    cur.fetchone.return_value = {
        "first_name": "Alice",
        "last_name": "Example",
        "email": "alice@example.com",
        "phone": None,
        "job_title": "Designer",
        "city": "Example City",
    }
    store.put_resource.return_value = {
        "id": "reference",
        "kind": "profile",
        "label": "Saved contact profile",
    }
    scope = Scope(tenant_id="test", owner_id="person:11111111-1111-1111-1111-111111111111")
    result = import_contact_profile(store, scope)
    assert "Alice" not in str(result) and "alice@example.com" not in str(result)
    assert cur.execute.call_args.args[1] == ("11111111-1111-1111-1111-111111111111", "test")
    saved = store.put_resource.call_args.args[1]
    assert json.loads(saved.payload.get_secret_value()) == {
        "first_name": "Alice",
        "last_name": "Example",
        "email": "alice@example.com",
        "occupation": "Designer",
        "city": "Example City",
    }


def test_contact_import_cannot_take_an_unlinked_owner_or_missing_record():
    store = MagicMock()
    with pytest.raises(PermissionError):
        import_contact_profile(store, Scope(tenant_id="test", owner_id="alice"))
    store.put_resource.assert_not_called()
    store.transaction.return_value.__enter__.return_value.fetchone.return_value = None
    with pytest.raises(PermissionError):
        import_contact_profile(
            store, Scope(tenant_id="test", owner_id="person:11111111-1111-1111-1111-111111111111")
        )
    store.put_resource.assert_not_called()
