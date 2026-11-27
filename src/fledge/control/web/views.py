"""HTML for the web view, built with htpy and made live with htmx.

Styles live in static/fledge.css.
"""

import json
from collections.abc import Sequence
from datetime import datetime

from htpy import (
    Element,
    Node,
    a,
    body,
    button,
    dd,
    div,
    dl,
    dt,
    form,
    fragment,
    h1,
    h2,
    head,
    header,
    html,
    img,
    li,
    link,
    main,
    meta,
    nav,
    option,
    p,
    script,
    section,
    select,
    span,
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
from pydantic import JsonValue

from fledge.control.store import CallStatus, RunStatus
from fledge.control.web.schemas import (
    CallOut,
    EventOut,
    GuidanceOut,
    RunDetail,
    RunOut,
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


def _result_text(result: JsonValue) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _link_or_text(value: JsonValue) -> Node:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return a(href=value)[value]
    return _result_text(value)


def _full_time(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _time(value: datetime | None) -> Node:
    """A short timestamp for tables, with the full one on hover."""
    if value is None:
        return "-"
    return span(title=_full_time(value))[value.strftime("%m-%d %H:%M:%S")]


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "-"
    minutes, seconds = divmod(int((end - start).total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _pill(status: RunStatus | CallStatus) -> Element:
    return span(f".pill.pill-{status}")[status]


def _page(page_title: str, content: Node) -> Element:
    return html(lang="en")[
        head[
            meta(charset="utf-8"),
            meta(name="viewport", content="width=device-width, initial-scale=1"),
            meta(name="htmx-config", content=_HTMX_CONFIG),
            title[page_title],
            link(rel="stylesheet", href="/static/fledge.css"),
            script(src=HTMX_URL, integrity=HTMX_INTEGRITY, crossorigin="anonymous"),
        ],
        body[
            header(".topbar")[
                div[
                    a(".brand", href="/")[
                        img(src="/static/logo.svg", alt=""), "fledge"
                    ],
                    nav[a(href="/")["Runs"]],
                ]
            ],
            main[content],
        ],
    ]


def index_page(runs: Sequence[RunOut], workflows: Sequence[str]) -> Element:
    rows = [
        tr[
            td[a(href=f"/runs/{run.id}")[run.title]],
            td[_pill(run.status)],
            td(".mono")[run.workflow],
            td(".mono")[run.origin],
            td(".mono")[run.external_id],
            td(".mono")[_time(run.created_at)],
            td(".mono")[_duration(run.started_at, run.finished_at)],
        ]
        for run in runs
    ]
    return _page(
        "fledge",
        [
            div(".page-head")[
                h1["Runs ", span(".muted")[str(len(runs))]],
                form("#launch", hx_post="/runs", hx_target="#launch-error")[
                    select(name="workflow", required=True)[
                        (option(value=name)[name] for name in workflows)
                    ],
                    input_(name="ref", placeholder="ticket ref", required=True),
                    button(type="submit")["Launch run"],
                ],
            ],
            p("#launch-error.error"),
            div(".card.flush")[
                table[
                    thead[
                        tr[
                            th["Title"],
                            th["Status"],
                            th["Workflow"],
                            th["Origin"],
                            th["Ref"],
                            th["Created"],
                            th["Duration"],
                        ]
                    ],
                    tbody[rows or tr[td(".muted", colspan="7")["No runs yet."]]],
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
    facts: dict[str, Node] = {
        "workflow": run.workflow,
        "origin": run.origin,
        "ref": run.external_id,
        "created": _time(run.created_at),
        "started": _time(run.started_at),
        "finished": _time(run.finished_at),
        "duration": _duration(run.started_at, run.finished_at),
    }
    return div(
        "#run-status.card",
        hx_get=f"/runs/{run.id}/live?after={after}",
        hx_trigger=trigger,
        hx_swap="outerHTML",
        hx_sync="this:drop",
    )[
        dl(".facts")[
            div[
                dt["status"],
                dd[
                    span(f"#status.pill.pill-{run.status}")[run.status],
                    " ",
                    span(
                        "#cancel-requested.muted",
                        hidden=not (active and run.cancel_requested),
                    )["(cancel requested)"],
                ],
            ],
            (div[dt[label], dd(".mono")[value]] for label, value in facts.items()),
            div[dt["ticket"], dd[_link_or_text(run.url)]],
            div[
                button(
                    class_="danger",
                    hx_post=f"/runs/{run.id}/cancel",
                    hx_target="#cancel-message",
                    # Without this it inherits outerHTML from #run-status and
                    # the empty success response would delete the message.
                    hx_swap="innerHTML",
                    id="cancel",
                    type="button",
                    hidden=not active,
                )["Cancel run"],
            ],
        ],
        p("#run-error.error")[run.error or ""],
    ]


def _calls_table(calls: Sequence[CallOut], *, oob: bool) -> Element:
    rows = [
        tr[
            td(".mono.muted")[str(call.position)],
            td(".mono")[call.name],
            td[call.kind],
            td[_pill(call.status)],
            td[_link_or_text(call.result)],
            td(".error")[call.error or ""],
            td(".mono")[_time(call.started_at)],
            td(".mono")[_duration(call.started_at, call.finished_at)],
        ]
        for call in calls
    ]
    return table("#calls", hx_swap_oob="true" if oob else False)[
        thead[
            tr[
                th["#"],
                th["Call"],
                th["Kind"],
                th["Status"],
                th["Result"],
                th["Error"],
                th["Started"],
                th["Duration"],
            ]
        ],
        tbody[rows or tr[td(".muted", colspan="8")["No calls yet."]]],
    ]


def _guidance_list(guidance: Sequence[GuidanceOut], *, oob: bool) -> Element:
    return ul("#guidance", hx_swap_oob="true" if oob else False)[
        (
            li[
                span(".mono.muted")[f"{item.author}/{item.source}"],
                " ",
                item.body,
                " ",
                span(".muted")["delivered" if item.delivered else "not delivered"],
            ]
            for item in guidance
        )
    ]


def _event_lines(events: Sequence[EventOut]) -> list[Element]:
    return [
        div[f"{_full_time(event.at)} [{event.kind}] {event.message}"]
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
            _run_status(run, _cursor(events, 0)),
            p("#cancel-message.error"),
            h2["Calls"],
            div(".card.flush")[_calls_table(detail.calls, oob=False)],
            div(".split")[
                section[
                    h2["Guidance"],
                    div(".card")[
                        _guidance_list(detail.guidance, oob=False),
                        form(
                            "#guidance-form",
                            {
                                "hx-on::after-request": (
                                    "if (event.detail.successful) this.reset()"
                                )
                            },
                            hx_post=f"/runs/{run.id}/guidance",
                            hx_target="#guidance-error",
                        )[
                            textarea(
                                name="body",
                                placeholder="Guidance for the agent",
                                required=True,
                            ),
                            button(type="submit")["Send guidance"],
                        ],
                        p("#guidance-error.error"),
                    ],
                ],
                section[
                    h2["Events"],
                    div("#events.card.log")[_event_lines(events)],
                ],
            ],
        ],
    )


def run_live(detail: RunDetail, events: Sequence[EventOut], after: int) -> Node:
    """What the polling element receives: itself, plus out-of-band updates."""
    return fragment[
        _run_status(detail.run, _cursor(events, after)),
        _calls_table(detail.calls, oob=True),
        _guidance_list(detail.guidance, oob=True),
        div(hx_swap_oob="beforeend:#events")[_event_lines(events)],
    ]
