"""Starts the control plane: the web app and the two loops, in one process."""

import asyncio
import logging

import uvicorn

from fledge.control.db import migrate
from fledge.control.orchestrator import Orchestrator
from fledge.control.poller import Poller
from fledge.control.runner.docker import DockerRunner
from fledge.control.settings import Settings
from fledge.control.snapshots import SnapshotLoader, start_triggers
from fledge.control.store import Store
from fledge.control.web.app import create_app
from fledge.triggers import TriggerCheckError
from fledge.workflows import load_workflows, read_snapshot

logger = logging.getLogger(__name__)


async def _serve(config: Settings, database_url: str, host: str, port: int) -> None:
    snapshot = read_snapshot(config.workflows_dir)
    loaded = load_workflows(snapshot.files)
    bindings = start_triggers(snapshot.hash, loaded.workflows)
    for name, binding in bindings.items():
        try:
            await binding.trigger.check()
        except TriggerCheckError as err:
            raise SystemExit(f"workflow {name!r}: {err}") from err
    logger.info(
        "loaded workflows %s from %s (snapshot %s)",
        ", ".join(sorted(loaded.workflows)),
        config.workflows_dir,
        snapshot.hash[:12],
    )
    async with Store(database_url) as store:
        await store.save_snapshot(snapshot.hash, snapshot.files)
        app = create_app(store, bindings)
        loader = SnapshotLoader(store)
        orchestrator = Orchestrator(
            store,
            DockerRunner(config.docker_network, source_dir=config.step_source_dir),
            config,
            loader.workflow,
        )
        poller = Poller(store, bindings, config)
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_config=None)
        )
        # A loop that dies takes the whole process down with it: a control
        # plane that serves the web view but no longer schedules is worse
        # than one that visibly crashed.
        async with asyncio.TaskGroup() as group:
            loops = [
                group.create_task(orchestrator.run_forever()),
                group.create_task(poller.run_forever()),
            ]
            await server.serve()
            for loop in loops:
                loop.cancel()


def serve(database_url: str, host: str, port: int) -> None:
    """Apply migrations, then run the control plane until it is stopped."""
    migrate(database_url)
    asyncio.run(_serve(Settings(), database_url, host, port))
