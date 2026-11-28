"""Moves runs through their workflows, deriving all state from the store and runner.

Each tick replays a running run's workflow function against its journal and
acts on the call it suspends on: a step is started in a container, a wait is
polled here.
"""

import asyncio
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import JsonValue

from fledge import checks, core, engine
from fledge.control.runner.base import ContainerState, Runner, StepSpec
from fledge.control.settings import Settings
from fledge.control.store import (
    CallRow,
    RunRow,
    Store,
    TerminalRunStatus,
    hash_token,
)
from fledge.wire import API_URL_ENV, READY_PORTS_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 2.0

LoadWorkflow = Callable[[str, str], Awaitable[engine.Workflow]]
"""Loads a workflow by (snapshot hash, workflow name)."""


class Orchestrator:
    def __init__(
        self,
        store: Store,
        runner: Runner,
        config: Settings,
        load_workflow: LoadWorkflow,
    ) -> None:
        # Alternatives such as ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN
        # share the default list, so an unset one is expected; a step that
        # needs it fails in its container with its own error.
        missing = [name for name in config.step_secrets if name not in os.environ]
        if missing:
            logger.warning(
                "step secrets not set, so not passed to steps: %s", ", ".join(missing)
            )
        self._secrets = {
            name: os.environ[name] for name in config.step_secrets if name in os.environ
        }
        self._store = store
        self._runner = runner
        self._config = config
        self._load_workflow = load_workflow
        # Only paces polling, so losing it on restart just means an early poll.
        self._last_polled: dict[UUID, float] = {}

    async def run_forever(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    async def tick(self) -> None:
        await self._cancel_pending()
        await self._promote_pending()
        for run in await self._store.list_runs_with_status("running"):
            # A run that cannot be reconciled (a runner error, say) must not
            # hold up every other run. It is retried on the next tick, since
            # all state is re-read from the store and runner.
            try:
                await self._reconcile(run)
            except Exception:
                logger.exception("reconciling run %s failed", run.id)

    async def _cancel_pending(self) -> None:
        for run in await self._store.list_runs_with_status("pending"):
            if run.cancel_requested and await self._store.finish_run(
                run.id, "cancelled"
            ):
                await self._store.append_event(run.id, "state", "run cancelled")

    async def _promote_pending(self) -> None:
        # Runs are not capped: only step containers are, when they start.
        for run in await self._store.list_runs_with_status("pending"):
            if await self._store.mark_run_running(run.id):
                await self._store.append_event(run.id, "state", "run started")

    async def _reconcile(self, run: RunRow) -> None:
        journal = await self._store.list_journal(run.id)
        latest = journal[-1] if journal else None
        if run.cancel_requested:
            await self._cancel_running(run, latest)
            return
        if latest is None:
            await self._runner.create_workspace(run.id)
        elif latest.kind == "step":
            if latest.status == "running":
                await self._check_running_step(latest)
                return
            await self._runner.stop_step(latest.id)
        await self._continue(run, journal, latest)

    async def _continue(
        self, run: RunRow, journal: Sequence[CallRow], latest: CallRow | None
    ) -> None:
        if run.started_at is None:
            raise RuntimeError(f"running run {run.id} has no start time")
        workflow = await self._load_workflow(run.snapshot_hash, run.workflow)
        outcome = engine.replay(
            workflow, core.Run(run.id, run.work), run.started_at, journal
        )
        match outcome:
            case engine.Succeeded():
                await self._end_run(run, latest, "succeeded", None)
            case engine.Failed(error):
                await self._end_run(run, latest, "failed", error)
            case engine.Cancelled():
                await self._end_run(run, latest, "cancelled", None)
            case engine.Suspended(call):
                await self._advance(run, call, latest)

    async def _advance(
        self, run: RunRow, call: engine.PendingCall, latest: CallRow | None
    ) -> None:
        if call.entry is not None and call.entry.status == "cancelled":
            await self._end_run(run, latest, "cancelled", None)
        elif call.kind == "step":
            await self._start_step(run, call)
        else:
            await self._wait(run, call)

    async def _cancel_running(self, run: RunRow, latest: CallRow | None) -> None:
        if latest is not None and latest.status == "running":
            if latest.kind == "step":
                await self._runner.stop_step(latest.id)
            # False means the step posted a result just before it was stopped,
            # and the internal API has already put that on the timeline.
            if await self._store.finish_call(latest.id, "cancelled"):
                await self._store.append_event(
                    run.id, "state", f"{_label(latest)} cancelled", latest.id
                )
            self._last_polled.pop(latest.id, None)
        await self._end_run(run, latest, "cancelled", None)

    async def _start_step(self, run: RunRow, call: engine.PendingCall) -> None:
        if call.entry is not None:
            raise RuntimeError(
                f"call {call.position} of run {run.id} is already started"
            )
        if await self._store.count_running_steps() >= self._config.max_concurrent_steps:
            return
        token = secrets.token_urlsafe(32)
        step_run = await self._store.create_call(
            run.id,
            call.position,
            "step",
            call.name,
            function=call.function,
            args=call.args,
            token_hash=hash_token(token),
        )
        env = {
            **self._secrets,
            **call.sandbox.env,
            API_URL_ENV: self._config.api_url,
            STEP_RUN_ID_ENV: str(step_run.id),
            STEP_TOKEN_ENV: token,
        }
        ready_ports = ",".join(
            f"{service.name}:{service.ready_port}"
            for service in call.sandbox.services
            if service.ready_port is not None
        )
        if ready_ports:
            env[READY_PORTS_ENV] = ready_ports
        await self._runner.start_step(
            StepSpec(
                step_run_id=step_run.id,
                run_id=run.id,
                image=self._config.image,
                env=env,
                services=call.sandbox.services,
            )
        )
        # The event follows the start so that the timeline only shows steps
        # whose container was started.
        await self._store.append_event(
            run.id, "state", f"step {call.name} started", step_run.id
        )

    async def _wait(self, run: RunRow, call: engine.PendingCall) -> None:
        entry = call.entry
        if entry is None:
            entry = await self._store.create_call(
                run.id,
                call.position,
                "wait",
                call.name,
                function=call.function,
                args=call.args,
            )
            await self._store.append_event(
                run.id, "state", f"waiting on {call.name}", entry.id
            )
        interval = (
            self._config.poll_interval_seconds
            if call.poll_interval is None
            else call.poll_interval.total_seconds()
        )
        now = time.monotonic()
        last = self._last_polled.get(entry.id)
        if last is not None and now - last < interval:
            return
        # Set before polling so that a poll that raises waits its turn too.
        self._last_polled[entry.id] = now
        try:
            result = await self._poll(run, call, entry.started_at)
        except Exception as err:
            # Retried at the next poll, but a failure that never clears (a
            # token without access, say) must be visible on the run's
            # timeline and not only in the control plane's log.
            await self._store.append_event(
                run.id,
                "log",
                f"{call.name} could not be checked, will retry: "
                f"{type(err).__name__}: {err}",
                entry.id,
            )
            raise
        if result is None:
            return
        if await self._store.finish_call(entry.id, "succeeded", result):
            await self._store.append_event(
                run.id, "state", f"{call.name} finished", entry.id
            )
        self._last_polled.pop(entry.id, None)

    async def _poll(
        self, run: RunRow, call: engine.PendingCall, started_at: datetime
    ) -> JsonValue | None:
        # Guidance from a person answers whatever the run is waiting on.
        guidance = await self._store.claim_guidance(run.id)
        if guidance:
            return checks.guidance_verdict([g.body for g in guidance])
        if call.poll is None:
            raise RuntimeError(f"wait {call.name} in run {run.id} has no poll")
        return await asyncio.to_thread(call.poll, started_at)

    async def _check_running_step(self, step_run: CallRow) -> None:
        state = await self._runner.step_state(step_run.id)
        if state is ContainerState.RUNNING:
            limit = timedelta(minutes=self._config.step_timeout_minutes)
            if datetime.now(UTC) - step_run.started_at > limit:
                await self._runner.stop_step(step_run.id)
                minutes = self._config.step_timeout_minutes
                await self._fail_step(
                    step_run, f"step timed out after {minutes} minutes"
                )
            return
        # The executor posts its result and then exits, so the result may have
        # landed after this tick listed the step runs.
        if (await self._reload(step_run)).status == "running":
            await self._fail_step(
                step_run, "step container exited without reporting a result"
            )

    async def _fail_step(self, step_run: CallRow, error: str) -> None:
        # The run carries on: the next replay raises StepFailed in the
        # workflow, which may catch it. False means the step posted a result
        # first; the next tick acts on it.
        if await self._store.finish_call(step_run.id, "failed", error=error):
            await self._store.append_event(
                step_run.run_id,
                "state",
                f"step {step_run.name} failed: {error}",
                step_run.id,
            )

    async def _reload(self, step_run: CallRow) -> CallRow:
        current = await self._store.get_call(step_run.id)
        if current is None:
            raise RuntimeError(f"step run {step_run.id} disappeared from the store")
        return current

    async def _end_run(
        self,
        run: RunRow,
        last: CallRow | None,
        status: TerminalRunStatus,
        error: str | None,
    ) -> None:
        # Cleanup comes before the status change so that a crash in between
        # leaves the run listed as running and the cleanup is repeated.
        if last is not None and last.kind == "step":
            await self._runner.stop_step(last.id)
        await self._runner.delete_workspace(run.id)
        if not await self._store.finish_run(run.id, status, error):
            return
        message = f"run {status}" + (f": {error}" if error else "")
        await self._store.append_event(run.id, "state", message)


def _label(step_run: CallRow) -> str:
    return f"step {step_run.name}" if step_run.kind == "step" else step_run.name
