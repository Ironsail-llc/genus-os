"""Warm-up reads private facts only for the person the run serves.

The interactive preamble is built for a run whose output may reach a shared
audience (a workspace, a group chat). The reader of private facts and evidence
is the run's served person (``robothor.engine.private_recall``), never the
sender's identity alone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine import warmup
from robothor.engine.private_recall import PrivateReader
from robothor.identity.context import IdentityContext

PERSON = "11111111-1111-1111-1111-111111111111"
IDENTITY = IdentityContext(
    tenant_id="test-tenant",
    channel="webchat",
    identifier="agent@example.com",
    verified=True,
    display_name="Alice Smith",
    person_id=PERSON,
    role="member",
)


@pytest.mark.parametrize(
    ("reader", "expected"),
    [(None, None), (PrivateReader(person_id=PERSON, user_id=None), PERSON)],
)
def test_the_preamble_passes_the_served_reader_to_entity_context(
    reader: PrivateReader | None, expected: str | None
) -> None:
    seen: dict[str, Any] = {}

    def _capture(message: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return ""

    with patch.object(warmup, "_build_entity_context", _capture):
        warmup.build_interactive_preamble(
            "main",
            "Any news from Dana Whitfield at Orion Freight?",
            include_blocks=False,
            tenant_id="test-tenant",
            identity=IDENTITY,
            private_reader=reader,
        )

    assert seen, "entity context was built"
    assert seen["sender_person_id"] == PERSON
    assert seen["reader_person_id"] == expected
