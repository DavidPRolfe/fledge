"""HTML for the web view, built with htpy and made live with htmx."""

import json
from collections.abc import Sequence
from datetime import datetime

from htpy import (
    Element,
    Node,
    a,
    body,
    button,
    div,
    form,
    fragment,
    h1,
    h2,
    head,
    html,
    li,
    meta,
    option,
    p,
    script,
    select,
    span,
    style,
    table,
    tbody,
    td,
    textarea,
    th,
    thead,
    title,
    tr,
    ul,
)
from htpy import input as input_
from markupsafe import Markup
from pydantic import JsonValue

from fledge.control.web.schemas import (
    EventOut,
    GuidanceOut,
    RunDetail,
    RunOut,
    StepRunOut,
)

HTMX_URL = "https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js"
HTMX_INTEGRITY = (
    "sha384-2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg"
)

# By default htmx ignores the body of 4xx responses. Swapping them lets the
# page routes answer a bad form with a 422 whose body is the message to show.
_HTMX_CONFIG = json.dumps(
    {
        "responseHandling": [
            {"code": "204", "swap": False},
            {"code": "[23]..", "swap": True},
            {"code": "[45]..", "swap": True, "error": True},
        ]
    }
)

# Cancel and guidance responses send this event so the run status refreshes at
# once instead of on the next poll.
REFRESH_EVENT = "refresh"

_POLL_SECONDS = 2
_ACTIVE_STATUSES = ("pending", "running")

_CSS = Markup("""
  body {
    font-family: system-ui, sans-serif;
    max-width: 960px;
    margin: 0 auto;
    padding: 1rem;
    color: #222;
    background: #fff;
    line-height: 1.4;
  }
  a { color: #0645ad; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; margin-top: 1.5rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.3rem 0.5rem; border-bottom: 1px solid #ddd; vertical-align: top; }
  form { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-start; }
  input, select, textarea, button { font: inherit; padding: 0.3rem 0.5rem; }
  textarea { flex: 1 1 20rem; min-height: 4rem; }
  .status { font-weight: bold; }
  .error { color: #b00020; white-space: pre-wrap; }
  .muted { color: #666; }
  #events {
    background: #f6f6f6;
    padding: 0.5rem;
    max-height: 30rem;
    overflow-y: auto;
    white-space: pre-wrap;
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
  }
""")


def _result_text(result: JsonValue) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _format_time(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _link_or_text(value: JsonValue) -> Node:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return a(href=value)[value]
    return _result_text(value)


def _page(page_title: str, content: Node) -> Element:
    return html(lang="en")[
        head[
            meta(charset="utf-8"),
            meta(name="viewport", content="width=device-width, initial-scale=1"),
            meta(name="htmx-config", content=_HTMX_CONFIG),
            title[page_title],
            style[_CSS],
            script(src=HTMX_URL, integrity=HTMX_INTEGRITY, crossorigin="anonymous"),
        ],
        body[p[a(href="/")["fledge"]], content],
    ]


def index_page(runs: Sequence[RunOut], workflows: Sequence[str]) -> Element:
    return _page(
        "fledge",
        [
            h1["Runs"],
            h2["Launch"],
            form("#launch", hx_post="/runs", hx_target="#launch-error")[
                select(name="workflow", required=True)[
                    (option(value=name)[name] for name in workflows)
                ],
                input_(name="ref", placeholder="ticket ref", required=True),
                button(type="submit")["Launch"],
            ],
            p("#launch-error.error"),
            table[
                thead[tr[th["Title"], th["Workflow"], th["Status"], th["Created"]]],
                tbody[
                    [
                        tr[
                            td[a(href=f"/runs/{run.id}")[run.title]],
                            td[run.workflow],
                            td(".status")[run.status],
                            td[_format_time(run.created_at)],
                        ]
                        for run in runs
                    ]
                    or tr[td(".muted", colspan="4")["No runs yet."]]
                ],
            ],
        ],
    )


def _run_status(run: RunOut, after: int) -> Element:
    """The part of the run page that polls for changes while the run is active.

    Each response replaces this element, carrying the event cursor forward and
    dropping the timer once the run has finished.
    """
    active = run.status in _ACTIVE_STATUSES
    trigger = f"{REFRESH_EVENT} from:body"
    if active:
        trigger = f"every {_POLL_SECONDS}s, {trigger}"
    return div(
        "#run-status",
        hx_get=f"/runs/{run.id}/live?after={after}",
        hx_trigger=trigger,
        hx_swap="outerHTML",
        hx_sync="this:drop",
    )[
        p[
            "Status: ",
            span("#status.status")[run.status],
            " ",
            span(
                "#cancel-requested.muted",
                hidden=not (active and run.cancel_requested),
            )["(cancel requested)"],
        ],
        p("#run-error.error")[run.error or ""],
        p[
            button(
                hx_post=f"/runs/{run.id}/cancel",
                hx_target="#cancel-message",
                # Without this it inherits outerHTML from #run-status and the
                # empty success response would delete the message element.
                hx_swap="innerHTML",
                id="cancel",
                type="button",
                hidden=not active,
            )["Cancel run"],
        ],
    ]


def _steps_table(steps: Sequence[StepRunOut], *, oob: bool) -> Element:
    return table("#steps", hx_swap_oob="true" if oob else False)[
        thead[tr[th["Call"], th["Kind"], th["Status"], th["Result"], th["Error"]]],
        tbody[
            (
                tr[
                    td[step.name],
                    td[step.kind],
                    td(".status")[step.status],
                    td[_link_or_text(step.result)],
                    td(".error")[step.error or ""],
                ]
                for step in steps
            )
        ],
    ]


def _guidance_list(guidance: Sequence[GuidanceOut], *, oob: bool) -> Element:
    return ul("#guidance", hx_swap_oob="true" if oob else False)[
        (
            li[
                f"{item.author} ({item.source}, "
                f"{'delivered' if item.delivered else 'not delivered'}): {item.body}"
            ]
            for item in guidance
        )
    ]


def _event_lines(events: Sequence[EventOut]) -> list[Element]:
    return [
        div[f"{_format_time(event.at)} [{event.kind}] {event.message}"]
        for event in events
    ]


def _cursor(events: Sequence[EventOut], after: int) -> int:
    return events[-1].id if events else after


def run_page(detail: RunDetail, events: Sequence[EventOut]) -> Element:
    run = detail.run
    return _page(
        f"{run.title} - fledge",
        [
            h1[run.title],
            p[
                "Ticket: ",
                _link_or_text(run.url),
                " ",
                span(".muted")[f"({run.workflow})"],
            ],
            _run_status(run, _cursor(events, 0)),
            p("#cancel-message.error"),
            h2["Calls"],
            _steps_table(detail.steps, oob=False),
            h2["Guidance"],
            _guidance_list(detail.guidance, oob=False),
            form(
                "#guidance-form",
                {"hx-on::after-request": "if (event.detail.successful) this.reset()"},
                hx_post=f"/runs/{run.id}/guidance",
                hx_target="#guidance-error",
            )[
                textarea(
                    name="body", placeholder="Guidance for the agent", required=True
                ),
                button(type="submit")["Send guidance"],
            ],
            p("#guidance-error.error"),
            h2["Events"],
            div("#events")[_event_lines(events)],
        ],
    )


def run_live(detail: RunDetail, events: Sequence[EventOut], after: int) -> Node:
    """What the polling element receives: itself, plus out-of-band updates."""
    return fragment[
        _run_status(detail.run, _cursor(events, after)),
        _steps_table(detail.steps, oob=True),
        _guidance_list(detail.guidance, oob=True),
        div(hx_swap_oob="beforeend:#events")[_event_lines(events)],
    ]
