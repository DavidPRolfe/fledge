"""The public API and web view, and the internal API for step containers."""

import hmac
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import jinja2
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from fledge.config import Config
from fledge.core import Trigger
from fledge.store import (
    EventKind,
    Guidance,
    GuidanceSource,
    Run,
    RunOrigin,
    RunStatus,
    StepRun,
    StepStatus,
    Store,
    hash_token,
)
from fledge.wire import EventsIn, GuidanceClaim, StepResult, StepRunContext

_TEMPLATES = Path(__file__).parent / "templates"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trigger: str
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
    name: str
    status: StepStatus
    output: dict[str, str]
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
    trigger: str
    ref: str


class GuidanceIn(BaseModel):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def create_app(
    store: Store, triggers: Mapping[str, Trigger], config: Config
) -> FastAPI:
    workflows = {trigger.name: trigger.workflow for trigger in config.trigger}
    if set(workflows) != set(triggers):
        raise ValueError(
            f"triggers {sorted(triggers)} do not match the configured "
            f"triggers {sorted(workflows)}"
        )
    app = FastAPI(title="fledge")
    app.include_router(_read_routes(store))
    app.include_router(_write_routes(store, triggers, workflows))
    app.include_router(_internal_routes(store))
    app.include_router(_web_routes(store, sorted(triggers)))
    return app


def _guidance_out(guidance: Guidance) -> GuidanceOut:
    return GuidanceOut(
        id=guidance.id,
        source=guidance.source,
        author=guidance.author,
        body=guidance.body,
        created_at=guidance.created_at,
        delivered=guidance.delivered_at is not None,
        delivered_at=guidance.delivered_at,
    )


async def _run_detail(store: Store, run_id: UUID) -> RunDetail:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return RunDetail(
        run=RunOut.model_validate(run),
        steps=[
            StepRunOut.model_validate(s) for s in await store.list_step_runs(run_id)
        ],
        guidance=[_guidance_out(g) for g in await store.list_guidance(run_id)],
    )


async def _existing_run(store: Store, run_id: UUID) -> Run:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


def _read_routes(store: Store) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/runs")
    async def list_runs() -> list[RunOut]:
        return [RunOut.model_validate(run) for run in await store.list_runs()]

    @router.get("/runs/{run_id}")
    async def get_run(run_id: UUID) -> RunDetail:
        return await _run_detail(store, run_id)

    @router.get("/runs/{run_id}/events")
    async def list_events(
        run_id: UUID, after: Annotated[int, Query(ge=0)] = 0
    ) -> list[EventOut]:
        await _existing_run(store, run_id)
        events = await store.list_events(run_id, after=after)
        return [EventOut.model_validate(event) for event in events]

    return router


def _write_routes(
    store: Store, triggers: Mapping[str, Trigger], workflows: Mapping[str, str]
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/runs", status_code=status.HTTP_201_CREATED)
    async def launch(body: LaunchIn) -> RunOut:
        # An unknown trigger name is a bad request body, like a bad ref, so
        # both are 422 rather than 404.
        trigger = triggers.get(body.trigger)
        if trigger is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"unknown trigger {body.trigger!r}",
            )
        try:
            work = await trigger.fetch(body.ref)
        except ValueError as err:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(err)
            ) from err
        run = await store.create_manual_run(body.trigger, workflows[body.trigger], work)
        return RunOut.model_validate(run)

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: UUID) -> RunOut:
        """Request cancel. 409 if the run has already finished."""
        await _existing_run(store, run_id)
        if not await store.request_cancel(run_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "run has already finished")
        return RunOut.model_validate(await _existing_run(store, run_id))

    @router.post("/runs/{run_id}/guidance", status_code=status.HTTP_201_CREATED)
    async def add_guidance(run_id: UUID, body: GuidanceIn) -> GuidanceOut:
        await _existing_run(store, run_id)
        guidance = await store.add_guidance(run_id, "web", "web", body.body)
        if guidance is None:
            # Only a repeated external_id makes the store skip the insert, and
            # web guidance has none.
            raise RuntimeError("guidance without an external id was not inserted")
        return _guidance_out(guidance)

    return router


def _step_run_auth(store: Store) -> Callable[..., Awaitable[StepRun]]:
    async def authorized_step_run(
        step_run_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> StepRun:
        unauthorized = HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
        if authorization is None:
            raise unauthorized
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise unauthorized
        step_run = await store.get_step_run(step_run_id)
        if step_run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "step run not found")
        if not hmac.compare_digest(hash_token(token), step_run.token_hash):
            raise unauthorized
        return step_run

    return authorized_step_run


def _internal_routes(store: Store) -> APIRouter:
    router = APIRouter(prefix="/internal/step-runs/{step_run_id}")
    authorized_step_run = _step_run_auth(store)

    @router.get("")
    async def context(
        step_run: Annotated[StepRun, Depends(authorized_step_run)],
    ) -> StepRunContext:
        run = await store.get_run(step_run.run_id)
        if run is None:
            raise RuntimeError(f"step run {step_run.id} has no run")
        earlier = [
            s
            for s in await store.list_step_runs(run.id)
            if s.step_index < step_run.step_index
        ]
        return StepRunContext(
            run_id=run.id,
            workflow=run.workflow,
            step_index=step_run.step_index,
            work=run.work,
            outputs={s.name: s.output for s in earlier},
        )

    @router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
    async def append_events(
        step_run: Annotated[StepRun, Depends(authorized_step_run)], body: EventsIn
    ) -> None:
        await store.append_events(
            step_run.run_id,
            [(event.kind, event.message) for event in body.events],
            step_run_id=step_run.id,
        )

    @router.post("/guidance/claim")
    async def claim_guidance(
        step_run: Annotated[StepRun, Depends(authorized_step_run)],
    ) -> GuidanceClaim:
        claimed = await store.claim_guidance(step_run.run_id)
        return GuidanceClaim(bodies=[guidance.body for guidance in claimed])

    @router.post("/result", status_code=status.HTTP_204_NO_CONTENT)
    async def report_result(
        step_run: Annotated[StepRun, Depends(authorized_step_run)], body: StepResult
    ) -> None:
        recorded = await store.finish_step_run(
            step_run.id, body.status, body.output, body.error
        )
        if not recorded:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "step run is no longer running; the result was not recorded",
            )

    return router


def _is_http_url(value: object) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat(sep=" ", timespec="seconds")


def _web_routes(store: Store, trigger_names: list[str]) -> APIRouter:
    router = APIRouter()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.tests["http_url"] = _is_http_url
    env.filters["time"] = _format_time

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        runs = [RunOut.model_validate(run) for run in await store.list_runs()]
        return env.get_template("index.html").render(runs=runs, triggers=trigger_names)

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: UUID) -> str:
        detail = await _run_detail(store, run_id)
        return env.get_template("run.html").render(
            run=detail.run, steps=detail.steps, guidance=detail.guidance
        )

    return router
