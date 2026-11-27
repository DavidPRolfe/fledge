import argparse
import logging
import os
import sys
from pathlib import Path

import httpx

from fledge.executor import execute_from_env
from fledge.scaffold import write_github_workflow

DATABASE_URL_ENV = "DATABASE_URL"


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
            from fledge.control.serve import serve  # noqa: PLC0415 - step containers run this CLI and must not load the control plane

            serve(os.environ[DATABASE_URL_ENV], args.host, args.port)
        case "init":
            try:
                path = write_github_workflow(args.dir, args.name, args.repo, args.label)
            except FileExistsError as err:
                raise SystemExit(f"{err.filename} already exists") from err
            except ValueError as err:
                raise SystemExit(str(err)) from err
            sys.stdout.write(f"wrote {path}\n")
        case "migrate":
            from fledge.control.db import migrate  # noqa: PLC0415 - step containers run this CLI and must not load the control plane

            migrate(os.environ[DATABASE_URL_ENV])
        case "step-exec":
            sys.exit(execute_from_env())
        case "submit":
            _submit(args.url, args.workflow, args.refs)
