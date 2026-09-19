"""Normalize portable operator review packets without publishing or fetching sources."""

import json
from typing import Literal

from pydantic import Field

from robothor.operations.store import digest
from robothor.sales.models import Contract, QualificationPolicy


class LibraryPacket(Contract):
    kind: Literal["qualification", "knowledge"]
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    data: dict


def preview(value):
    packet = LibraryPacket.model_validate(value).model_dump(mode="json")
    if len(json.dumps(packet).encode()) > 131072:
        raise ValueError("Library packet exceeds 128 KiB")
    data = packet["data"]
    if packet["kind"] == "qualification":
        policy = QualificationPolicy.model_validate(data)
        if policy.version != packet["version"]:
            raise ValueError("Packet and qualification versions must match")
        if set(policy.criteria_definitions) != set(policy.weights) or any(
            not definition.strip() for definition in policy.criteria_definitions.values()
        ):
            raise ValueError("Define the evidence required for every scored criterion")
        packet["data"] = policy.model_dump(mode="json")
    else:
        claims = data.get("claims")
        if (
            not isinstance(claims, dict)
            or not 1 <= len(claims) <= 100
            or any(
                not isinstance(key, str)
                or not key.strip()
                or len(key) > 100
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 4000
                for key, text in claims.items()
            )
        ):
            raise ValueError("Provide 1–100 named, nonempty text claims")
    return {"packet": packet, "content_hash": digest(packet)}
