"""The public API used by the web view and the CLI. Unauthenticated in the MVP."""

from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from fledge.control.snapshots import TriggerBinding
from fledge.control.store import Store
from fledge.control.web.schemas import (
    EventOut,
    GuidanceIn,
    GuidanceOut,
    LaunchIn,
    RunDetail,
    RunOut,
    existing_run,
    guidance_out,
    run_detail,
)


async def _launch(
    store: Store, workflows: Mapping[str, TriggerBinding], body: LaunchIn
) -> RunOut:
    # An unknown workflow name is a bad request body, like a bad ref, so both
    # are 422 rather than 404.
    binding = workflows.get(body.workflow)
    if binding is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown workflow {body.workflow!r}",
        )
    try:
        work = await binding.trigger.fetch(body.ref)
    except ValueError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(err)) from err
    run = await store.create_manual_run(binding.workflow, work)
    return RunOut.model_validate(run)


def public_routes(store: Store, workflows: Mapping[str, TriggerBinding]) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/runs")
    async def list_runs() -> list[RunOut]:
        return [RunOut.model_validate(run) for run in await store.list_runs()]

    @router.get("/runs/{run_id}")
    async def get_run(run_id: UUID) -> RunDetail:
        return await run_detail(store, run_id)

    @router.get("/runs/{run_id}/events")
    async def list_events(
        run_id: UUID, after: Annotated[int, Query(ge=0)] = 0
    ) -> list[EventOut]:
        await existing_run(store, run_id)
        events = await store.list_events(run_id, after=after)
        return [EventOut.model_validate(event) for event in events]

    @router.post("/runs", status_code=status.HTTP_201_CREATED)
    async def launch(body: LaunchIn) -> RunOut:
        return await _launch(store, workflows, body)

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: UUID) -> RunOut:
        """Request cancel. 409 if the run has already finished."""
        await existing_run(store, run_id)
        if not await store.request_cancel(run_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "run has already finished")
        return RunOut.model_validate(await existing_run(store, run_id))

    @router.post("/runs/{run_id}/guidance", status_code=status.HTTP_201_CREATED)
    async def add_guidance(run_id: UUID, body: GuidanceIn) -> GuidanceOut:
        await existing_run(store, run_id)
        guidance = await store.add_guidance(run_id, "web", "web", body.body)
        if guidance is None:
            # Only a repeated external_id makes the store skip the insert, and
            # web guidance has none.
            raise RuntimeError("guidance without an external id was not inserted")
        return guidance_out(guidance)

    return router
