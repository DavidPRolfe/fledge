"""The server-rendered web view."""

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import jinja2
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import JsonValue

from fledge.store import Store
from fledge.web.schemas import RunOut, run_detail

_TEMPLATES = Path(__file__).parent / "templates"


def _is_http_url(value: object) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _result_text(result: JsonValue) -> str:
    """Matches renderSteps in run.html, which redraws the table as it changes."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat(sep=" ", timespec="seconds")


def page_routes(store: Store, workflow_names: list[str]) -> APIRouter:
    router = APIRouter()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.tests["http_url"] = _is_http_url
    env.filters["time"] = _format_time
    env.filters["result_text"] = _result_text

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        runs = [RunOut.model_validate(run) for run in await store.list_runs()]
        return env.get_template("index.html").render(
            runs=runs, workflows=workflow_names
        )

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: UUID) -> str:
        detail = await run_detail(store, run_id)
        return env.get_template("run.html").render(
            run=detail.run, steps=detail.steps, guidance=detail.guidance
        )

    return router
