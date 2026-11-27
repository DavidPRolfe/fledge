"""The server-rendered web view.

Forms post here rather than to the JSON API so that htmx gets HTML back: a
message to show on failure, or a redirect or refresh on success.
"""

from collections.abc import Mapping
from typing import Annotated
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from fledge.control.snapshots import TriggerBinding
from fledge.control.store import Store
from fledge.control.web import views
from fledge.control.web.public_api import launch_run
from fledge.control.web.schemas import (
    EventOut,
    GuidanceIn,
    LaunchIn,
    RunOut,
    existing_run,
    run_detail,
)

_REFRESH = {"HX-Trigger": views.REFRESH_EVENT}


async def _form[M: BaseModel](request: Request, model: type[M]) -> M:
    """Parse a urlencoded form; FastAPI's Form() would need python-multipart."""
    fields = dict(parse_qsl((await request.body()).decode()))
    try:
        return model.model_validate(fields)
    except ValidationError as err:
        message = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in err.errors()
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message) from err


def _message(err: HTTPException) -> HTMLResponse:
    return HTMLResponse(str(err.detail), status_code=err.status_code)


async def _events(store: Store, run_id: UUID, after: int) -> list[EventOut]:
    return [
        EventOut.model_validate(event)
        for event in await store.list_events(run_id, after=after)
    ]


def page_routes(store: Store, workflows: Mapping[str, TriggerBinding]) -> APIRouter:
    router = APIRouter(default_response_class=HTMLResponse)
    workflow_names = sorted(workflows)

    @router.get("/")
    async def index() -> str:
        runs = [RunOut.model_validate(run) for run in await store.list_runs()]
        return str(views.index_page(runs, workflow_names))

    @router.post("/runs")
    async def launch(request: Request) -> Response:
        try:
            run = await launch_run(store, workflows, await _form(request, LaunchIn))
        except HTTPException as err:
            return _message(err)
        return Response(
            status_code=status.HTTP_201_CREATED,
            headers={"HX-Redirect": f"/runs/{run.id}"},
        )

    @router.get("/runs/{run_id}")
    async def run_page(run_id: UUID) -> str:
        detail = await run_detail(store, run_id)
        return str(views.run_page(detail, await _events(store, run_id, 0)))

    @router.get("/runs/{run_id}/live")
    async def run_live(run_id: UUID, after: Annotated[int, Query(ge=0)] = 0) -> str:
        detail = await run_detail(store, run_id)
        events = await _events(store, run_id, after)
        return str(views.run_live(detail, events, after))

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: UUID) -> Response:
        await existing_run(store, run_id)
        if not await store.request_cancel(run_id):
            return HTMLResponse(
                "run has already finished",
                status_code=status.HTTP_409_CONFLICT,
                headers=_REFRESH,
            )
        return HTMLResponse("", headers=_REFRESH)

    @router.post("/runs/{run_id}/guidance")
    async def add_guidance(run_id: UUID, request: Request) -> Response:
        await existing_run(store, run_id)
        try:
            body = await _form(request, GuidanceIn)
        except HTTPException as err:
            return _message(err)
        await store.add_guidance(run_id, "web", "web", body.body)
        return HTMLResponse("", status_code=status.HTTP_201_CREATED, headers=_REFRESH)

    return router
