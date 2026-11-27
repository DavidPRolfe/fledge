"""Response and request bodies of the public API, and how they are read from the store."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints

from fledge.control.store import (
    CallKind,
    EventKind,
    Guidance,
    GuidanceSource,
    RunOrigin,
    RunRow,
    RunStatus,
    StepStatus,
    Store,
)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    origin: RunOrigin
    workflow: str
    title: str
    body: str
    url: str
    repo_url: str
    status: RunStatus
    cancel_requested: bool
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StepRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    step_index: int
    kind: CallKind
    name: str
    status: StepStatus
    result: JsonValue
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class GuidanceOut(BaseModel):
    id: UUID
    source: GuidanceSource
    author: str
    body: str
    created_at: datetime
    delivered: bool
    delivered_at: datetime | None


class RunDetail(BaseModel):
    run: RunOut
    steps: list[StepRunOut]
    guidance: list[GuidanceOut]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_run_id: UUID | None
    at: datetime
    kind: EventKind
    message: str


class LaunchIn(BaseModel):
    workflow: str
    ref: str


class GuidanceIn(BaseModel):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def guidance_out(guidance: Guidance) -> GuidanceOut:
    return GuidanceOut(
        id=guidance.id,
        source=guidance.source,
        author=guidance.author,
        body=guidance.body,
        created_at=guidance.created_at,
        delivered=guidance.delivered_at is not None,
        delivered_at=guidance.delivered_at,
    )


async def run_detail(store: Store, run_id: UUID) -> RunDetail:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return RunDetail(
        run=RunOut.model_validate(run),
        steps=[StepRunOut.model_validate(s) for s in await store.list_journal(run_id)],
        guidance=[guidance_out(g) for g in await store.list_guidance(run_id)],
    )


async def existing_run(store: Store, run_id: UUID) -> RunRow:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run
