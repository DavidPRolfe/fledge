"""Moves runs through their steps, deriving all state from the store and runner."""

import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from fledge.runner.base import ContainerState, Runner, StepSpec
from fledge.settings import Settings
from fledge.store import Run, StepRun, Store, TerminalRunStatus, hash_token
from fledge.wire import API_URL_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 2.0


class Orchestrator:
    def __init__(
        self,
        store: Store,
        runner: Runner,
        config: Settings,
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

    async def run_forever(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    async def tick(self) -> None:
        await self._cancel_pending()
        await self._promote_pending()
        for run in await self._store.list_runs_with_status("running"):
            # A run that cannot be reconciled (a runner error, say) must not hold up every other run. It is retried on the
            # next tick, since all state is re-read from the store and runner.
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
        running = len(await self._store.list_runs_with_status("running"))
        for run in await self._store.list_runs_with_status("pending"):
            if running >= self._config.max_concurrent_runs:
                return
            if await self._store.mark_run_running(run.id):
                running += 1
                await self._store.append_event(run.id, "state", "run started")

    async def _reconcile(self, run: Run) -> None:
        step_runs = await self._store.list_step_runs(run.id)
        latest = step_runs[-1] if step_runs else None
        if run.cancel_requested:
            await self._cancel_running(run, latest)
            return
        if latest is None:
            await self._runner.create_workspace(run.id)
            await self._start_step(run, 0, None)
            return
        match latest.status:
            case "running":
                await self._check_running_step(run, latest)
            case "succeeded":
                await self._advance(run, latest)
            case "failed":
                await self._end_run(run, latest, "failed", latest.error)
            case "cancelled":
                await self._end_run(run, latest, "cancelled", None)

    async def _cancel_running(self, run: Run, latest: StepRun | None) -> None:
        if latest is not None and latest.status == "running":
            await self._runner.stop_step(latest.id)
            await self._store.finish_step_run(latest.id, "cancelled", {})
            # Re-read so the timeline reports what was actually recorded, which
            # may be a result the step posted just before it was stopped.
            latest = await self._reload(latest)
        await self._end_run(run, latest, "cancelled", None)

    async def _advance(self, run: Run, finished: StepRun) -> None:
        if finished.step_index + 1 >= len(_steps(run)):
            await self._end_run(run, finished, "succeeded", None)
            return
        await self._runner.stop_step(finished.id)
        await self._start_step(run, finished.step_index + 1, finished)

    async def _start_step(self, run: Run, index: int, previous: StepRun | None) -> None:
        name = _steps(run)[index]
        token = secrets.token_urlsafe(32)
        step_run = await self._store.create_step_run(
            run.id, index, name, hash_token(token)
        )
        env = {
            **self._secrets,
            API_URL_ENV: self._config.api_url,
            STEP_RUN_ID_ENV: str(step_run.id),
            STEP_TOKEN_ENV: token,
        }
        await self._runner.start_step(
            StepSpec(
                step_run_id=step_run.id,
                run_id=run.id,
                image=self._config.image,
                env=env,
            )
        )
        # Events follow the start so that a start retried after an error
        # does not repeat them on the timeline.
        if previous is not None:
            await self._store.append_event(
                run.id, "state", _step_outcome(previous), previous.id
            )
        await self._store.append_event(
            run.id, "state", f"step {name} started", step_run.id
        )

    async def _check_running_step(self, run: Run, step_run: StepRun) -> None:
        state = await self._runner.step_state(step_run.id)
        if state is ContainerState.RUNNING:
            limit = timedelta(minutes=self._config.step_timeout_minutes)
            if datetime.now(UTC) - step_run.started_at > limit:
                await self._runner.stop_step(step_run.id)
                minutes = self._config.step_timeout_minutes
                await self._fail_step(
                    run, step_run, f"step timed out after {minutes} minutes"
                )
            return
        # The executor posts its result and then exits, so the result may have
        # landed after this tick listed the step runs.
        if (await self._reload(step_run)).status == "running":
            await self._fail_step(
                run, step_run, "step container exited without reporting a result"
            )

    async def _fail_step(self, run: Run, step_run: StepRun, error: str) -> None:
        # False means the step posted a result first; the next tick acts on it.
        if await self._store.finish_step_run(step_run.id, "failed", {}, error):
            await self._end_run(run, await self._reload(step_run), "failed", error)

    async def _reload(self, step_run: StepRun) -> StepRun:
        current = await self._store.get_step_run(step_run.id)
        if current is None:
            raise RuntimeError(f"step run {step_run.id} disappeared from the store")
        return current

    async def _end_run(
        self,
        run: Run,
        last: StepRun | None,
        status: TerminalRunStatus,
        error: str | None,
    ) -> None:
        # Cleanup comes before the status change so that a crash in between
        # leaves the run listed as running and the cleanup is repeated.
        if last is not None:
            await self._runner.stop_step(last.id)
        await self._runner.delete_workspace(run.id)
        if not await self._store.finish_run(run.id, status, error):
            return
        if last is not None:
            await self._store.append_event(
                run.id, "state", _step_outcome(last), last.id
            )
        message = f"run {status}" + (f": {error}" if error else "")
        await self._store.append_event(run.id, "state", message)


def _steps(run: Run) -> list[str]:
    if run.steps is None:
        raise RuntimeError(f"run {run.id} predates workflow snapshots")
    return run.steps


def _step_outcome(step_run: StepRun) -> str:
    message = f"step {step_run.name} {step_run.status}"
    return message + (f": {step_run.error}" if step_run.error else "")
