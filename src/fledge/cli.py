import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
import uvicorn

from fledge.core import TriggerCheckError
from fledge.db import migrate
from fledge.executor import execute_from_env
from fledge.orchestrator import Orchestrator
from fledge.poller import Poller
from fledge.runner.docker import DockerRunner
from fledge.settings import Settings
from fledge.store import Store
from fledge.web.app import create_app
from fledge.workflows import (
    SnapshotLoader,
    load_workflows,
    read_snapshot,
    start_triggers,
    write_github_workflow,
)

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "DATABASE_URL"


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


def _submit(url: str, workflow: str, refs: list[str]) -> None:
    with httpx.Client(base_url=url) as client:
        for ref in refs:
            response = client.post("/api/runs", json={"workflow": workflow, "ref": ref})
            if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
                raise SystemExit(f"{ref}: {response.json()['detail']}")
            response.raise_for_status()
            run_id = response.json()["id"]
            sys.stdout.write(f"{ref}: {url}/runs/{run_id}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fledge")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the control plane")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - runs in a container; compose publishes it on localhost only
    serve.add_argument("--port", type=int, default=8000)

    init = commands.add_parser(
        "init", help="write a workflow that turns labelled GitHub issues into PRs"
    )
    init.add_argument("--repo", required=True, help="GitHub repository, owner/name")
    init.add_argument("--label", default="fledge", help="issue label to watch")
    init.add_argument("--name", default="tickets", help="workflow name")
    init.add_argument("--dir", type=Path, default=Path("workflows"))

    commands.add_parser("migrate", help="apply database migrations")
    commands.add_parser("step-exec", help="run one step (inside a step container)")

    submit = commands.add_parser("submit", help="launch runs for tickets by hand")
    submit.add_argument("--workflow", required=True, help="workflow name")
    submit.add_argument("--url", default="http://localhost:8000")
    submit.add_argument("refs", nargs="+", metavar="ref")
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    match args.command:
        case "serve":
            database_url = os.environ[DATABASE_URL_ENV]
            migrate(database_url)
            asyncio.run(_serve(Settings(), database_url, args.host, args.port))
        case "init":
            try:
                path = write_github_workflow(args.dir, args.name, args.repo, args.label)
            except FileExistsError as err:
                raise SystemExit(f"{err.filename} already exists") from err
            except ValueError as err:
                raise SystemExit(str(err)) from err
            sys.stdout.write(f"wrote {path}\n")
        case "migrate":
            migrate(os.environ[DATABASE_URL_ENV])
        case "step-exec":
            sys.exit(execute_from_env())
        case "submit":
            _submit(args.url, args.workflow, args.refs)
