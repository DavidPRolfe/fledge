"""The internal API for step containers. The contract is in fledge.wire."""

import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from fledge.store import StepRun, Store, hash_token
from fledge.wire import EventsIn, GuidanceClaim, StepResult, StepRunContext


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


def internal_routes(store: Store) -> APIRouter:
    router = APIRouter(prefix="/internal/step-runs/{step_run_id}")
    authorized_step_run = _step_run_auth(store)

    @router.get("")
    async def context(
        step_run: Annotated[StepRun, Depends(authorized_step_run)],
    ) -> StepRunContext:
        run = await store.get_run(step_run.run_id)
        if run is None:
            raise RuntimeError(f"step run {step_run.id} has no run")
        if run.snapshot_hash is None:
            raise RuntimeError(f"run {run.id} predates workflow snapshots")
        return StepRunContext(
            run_id=run.id,
            work=run.work,
            function=step_run.function,
            args=step_run.args,
            workflow_files=await store.get_snapshot_files(run.snapshot_hash),
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
            step_run.id, body.status, body.result, body.error
        )
        if not recorded:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "step run is no longer running; the result was not recorded",
            )
        if body.status == "succeeded":
            message = f"step {step_run.name} succeeded"
        else:
            message = f"step {step_run.name} failed: {body.error}"
        await store.append_event(
            step_run.run_id, "state", message, step_run_id=step_run.id
        )

    return router
