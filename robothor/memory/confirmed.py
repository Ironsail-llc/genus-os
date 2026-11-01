"""Operator-confirmed facts: the one predicate every retiring path shares.

An operator's edit marks a fact ``metadata.operator_confirmed = {at, by}``.
That fact is the operator's word, so no automatic path may retire it: conflict
resolution, consolidation, the quality prune, dechurn and resolution capture
all leave it active. Python callers test a row with :func:`is_operator_confirmed`;
SQL callers add :data:`NOT_OPERATOR_CONFIRMED_SQL` to their ``WHERE``. Both
treat a missing key and a JSON ``null`` as "not confirmed" and any other value
as confirmed, so the two can never disagree about a row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# True for NULL metadata, an object without the key, and an explicit JSON null.
NOT_OPERATOR_CONFIRMED_SQL = (
    "COALESCE(metadata->'operator_confirmed', 'null'::jsonb) = 'null'::jsonb"
)


def is_operator_confirmed(row: Mapping[str, Any]) -> bool:
    """Whether ``row['metadata']`` carries an ``operator_confirmed`` value.

    ``metadata`` may be a dict (psycopg2's JSONB decode), a JSON string, or
    absent; anything that does not decode to an object is not confirmed.
    """
    metadata: Any = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    if not isinstance(metadata, Mapping):
        return False
    return metadata.get("operator_confirmed") is not None
