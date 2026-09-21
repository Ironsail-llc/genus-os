"""Reference-only requests for the private workflow service."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, TypeAdapter, field_validator

from robothor.autonomy.broker import ExecutionPlan, url_origin
from robothor.autonomy.models import StrictModel

AUDIENCE = "genus-autonomy"
SCOPE = "autonomy:workflow"


class OpenRequest(StrictModel):
    kind: Literal["open"]
    operation_id: UUID
    url: str = Field(max_length=2000)
    session_resource_id: UUID | None = None

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url_origin(value)
        return value


class InspectRequest(StrictModel):
    kind: Literal["inspect", "status", "close"]
    workflow_id: UUID


class ExecuteRequest(StrictModel):
    kind: Literal["execute"]
    workflow_id: UUID
    command_id: UUID
    revision: int = Field(ge=0)
    plan: ExecutionPlan
    advance: bool = False
    verification_code: SecretStr | None = None


class ReconcileRequest(StrictModel):
    kind: Literal["reconcile"]
    workflow_id: UUID
    command_id: UUID
    revision: int = Field(ge=0)
    selector: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=3, max_length=300)


RPC: TypeAdapter[OpenRequest | InspectRequest | ExecuteRequest | ReconcileRequest] = TypeAdapter(
    Annotated[
        OpenRequest | InspectRequest | ExecuteRequest | ReconcileRequest,
        Field(discriminator="kind"),
    ]
)
