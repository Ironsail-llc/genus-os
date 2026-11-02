"""No test inherits another test's identity enrichment.

``enrich_identity_for_session`` holds a caller's enrichment for the chat
session, in a module-level cache. Across tests that is wrong: a later test that
patches ``enrich_identity`` for the same caller is served the held result and
never reaches its patch. Both tests below start by asserting the caches are
empty and then fill them, so whichever runs second fails unless the suite
clears them around every test.
"""

from __future__ import annotations

from unittest.mock import patch

from robothor.identity import enrichment
from robothor.identity.context import EnrichedIdentity, IdentityContext

CALLER = IdentityContext(
    tenant_id="test-tenant",
    channel="webchat",
    identifier="user-1",
    verified=True,
    display_name="Alice",
    role="owner",
    person_id="person-1",
)


def _assert_empty_then_fill() -> None:
    assert not enrichment._session_cache, "a previous test's session enrichment leaked in"
    assert not enrichment._cache, "a previous test's enrichment leaked in"
    with patch(
        "robothor.identity.enrichment._build_enrichment",
        return_value=EnrichedIdentity(company="Example Co"),
    ):
        assert enrichment.enrich_identity_for_session("main", CALLER) is not None
    assert enrichment._session_cache


def test_a_session_enrichment_does_not_reach_the_next_test() -> None:
    _assert_empty_then_fill()


def test_nor_does_the_next_one_reach_the_one_after() -> None:
    _assert_empty_then_fill()


def test_a_fresh_patch_of_enrich_identity_is_reached() -> None:
    """The reported pair: a patched ``enrich_identity`` is called, not bypassed."""
    with patch("robothor.identity.enrichment.enrich_identity", return_value=None) as mock_enrich:
        enrichment.enrich_identity_for_session("main", CALLER)
    mock_enrich.assert_called_once()
    with patch(
        "robothor.identity.enrichment._build_enrichment",
        return_value=EnrichedIdentity(company="Example Co"),
    ):
        enrichment.enrich_identity_for_session("main", CALLER)
