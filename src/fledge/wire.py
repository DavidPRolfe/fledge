"""The contract between a step container and the control plane's internal API.

Routes, all under the bearer token in STEP_TOKEN_ENV:
    GET  /internal/step-runs/{id}                  returns StepRunContext
    POST /internal/step-runs/{id}/events           body EventsIn
    POST /internal/step-runs/{id}/guidance/claim   returns GuidanceClaim
    POST /internal/step-runs/{id}/result           body StepResult
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from fledge.core import WorkItem

API_URL_ENV = "FLEDGE_API_URL"
STEP_RUN_ID_ENV = "FLEDGE_STEP_RUN_ID"
STEP_TOKEN_ENV = "FLEDGE_STEP_TOKEN"  # noqa: S105 - name of an env var, not a secret

WORKSPACE_PATH = "/workspace"


class StepRunContext(BaseModel):
    run_id: UUID
    workflow: str
    step_index: int
    work: WorkItem
    outputs: dict[str, dict[str, str]]
    workflow_files: dict[str, str]
    """The run's workflow snapshot: source text by relative path."""


class EventIn(BaseModel):
    kind: Literal["log", "agent"]
    message: str


class EventsIn(BaseModel):
    events: list[EventIn]


class GuidanceClaim(BaseModel):
    bodies: list[str]


class StepResult(BaseModel):
    status: Literal["succeeded", "failed"]
    output: dict[str, str] = {}
    error: str | None = None
