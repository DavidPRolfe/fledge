import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
import uvicorn

from fledge.config import Config, load_config
from fledge.db import migrate
from fledge.executor import execute_from_env
from fledge.orchestrator import Orchestrator
from fledge.poller import Poller, load_triggers
from fledge.registry import load_workflow
from fledge.runner.docker import DockerRunner
from fledge.store import Store
from fledge.web.app import create_app

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "DATABASE_URL"


async def _serve(config: Config, database_url: str, host: str, port: int) -> None:
    bindings = load_triggers(config)
    async with Store(database_url) as store:
        app = create_app(
            store, {name: b.trigger for name, b in bindings.items()}, config
        )
        orchestrator = Orchestrator(
            store,
            DockerRunner(config.docker_network, source_dir=config.step_source_dir),
            config,
            load_workflow,
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


def _submit(url: str, trigger: str, refs: list[str]) -> None:
    with httpx.Client(base_url=url) as client:
        for ref in refs:
            response = client.post("/api/runs", json={"trigger": trigger, "ref": ref})
            if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
                raise SystemExit(f"{ref}: {response.json()['detail']}")
            response.raise_for_status()
            run_id = response.json()["id"]
            sys.stdout.write(f"{ref}: {url}/runs/{run_id}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fledge")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the control plane")
    serve.add_argument("--config", type=Path, default=Path("fledge.toml"))
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - runs in a container; compose publishes it on localhost only
    serve.add_argument("--port", type=int, default=8000)

    commands.add_parser("migrate", help="apply database migrations")
    commands.add_parser("step-exec", help="run one step (inside a step container)")

    submit = commands.add_parser("submit", help="launch runs for tickets by hand")
    submit.add_argument("--trigger", required=True, help="trigger name in fledge.toml")
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
            asyncio.run(
                _serve(load_config(args.config), database_url, args.host, args.port)
            )
        case "migrate":
            migrate(os.environ[DATABASE_URL_ENV])
        case "step-exec":
            sys.exit(execute_from_env())
        case "submit":
            _submit(args.url, args.trigger, args.refs)
