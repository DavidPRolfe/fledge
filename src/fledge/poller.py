"""Turns trigger work items into runs and ticket comments into guidance."""

import asyncio
import logging
from collections.abc import Mapping

from fledge.settings import Settings
from fledge.store import Run, Store
from fledge.workflows import TriggerBinding

logger = logging.getLogger(__name__)


class Poller:
    def __init__(
        self, store: Store, triggers: Mapping[str, TriggerBinding], config: Settings
    ) -> None:
        self._store = store
        self._triggers = triggers
        self._interval = config.poll_interval_seconds

    async def run_forever(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    async def poll_once(self) -> None:
        active = [
            *await self._store.list_runs_with_status("pending"),
            *await self._store.list_runs_with_status("running"),
        ]
        for name, binding in self._triggers.items():
            runs = [run for run in active if run.workflow == name]
            # Triggers talk to outside services that fail independently, so
            # one being down must not stop the others or end the loop. This is
            # the only place the poller swallows an error.
            try:
                await self._poll_trigger(name, binding, runs)
            except Exception:
                logger.exception("polling the trigger of workflow %r failed", name)

    async def _poll_trigger(
        self, name: str, binding: TriggerBinding, runs: list[Run]
    ) -> None:
        for item in await binding.trigger.poll():
            run = await self._store.create_polled_run(binding.workflow, item)
            if run is not None:
                logger.info("created run %s for %s %r", run.id, name, item.external_id)
        for run in runs:
            comments = await binding.trigger.poll_guidance(
                run.external_id, since=run.created_at
            )
            for comment in comments:
                await self._store.add_guidance(
                    run.id,
                    "trigger",
                    comment.author,
                    comment.body,
                    external_id=comment.external_id,
                )
