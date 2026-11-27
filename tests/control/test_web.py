from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import JsonValue

from fledge.control.snapshots import TriggerBinding
from fledge.control.store import CallRow, RunRow, Store, WorkflowRef, hash_token
from fledge.control.web.app import create_app
from fledge.core import WorkItem
from fledge.triggers import Comment

pytestmark = pytest.mark.anyio

UNTRUSTED_TITLE = "<script>alert(1)</script>"
SNAPSHOT_FILES = {"ticket_to_pr.py": "# workflow source"}
TICKET_TO_PR = WorkflowRef("ticket_to_pr", "snapshot-under-test")


def work_item(external_id: str, title: str = "Fix the thing") -> WorkItem:
    return WorkItem(
        external_id=external_id,
        title=title,
        body="Body",
        url=f"https://example.test/issues/{external_id}",
        repo_url="https://example.test/repo.git",
    )


class FakeTrigger:
    def __init__(self, items: dict[str, WorkItem]) -> None:
        self.items = items

    def check(self) -> None:
        pass

    def poll(self) -> list[WorkItem]:
        return list(self.items.values())

    def fetch(self, ref: str) -> WorkItem:
        if ref not in self.items:
            raise ValueError(f"no issue {ref}")
        return self.items[ref]

    def poll_guidance(self, item_external_id: str, since: datetime) -> list[Comment]:
        return []


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with Store(database_url) as store:
        await store.save_snapshot(TICKET_TO_PR.snapshot_hash, SNAPSHOT_FILES)
        yield store


@pytest.fixture
async def client(store: Store) -> AsyncIterator[httpx.AsyncClient]:
    trigger = FakeTrigger({"7": work_item("7"), "8": work_item("8", UNTRUSTED_TITLE)})
    app = create_app(store, {"ticket_to_pr": TriggerBinding(trigger, TICKET_TO_PR)})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def make_run(store: Store, title: str = "Fix the thing") -> RunRow:
    return await store.create_manual_run(TICKET_TO_PR, work_item("1", title))


async def make_step(
    store: Store,
    run: RunRow,
    index: int = 0,
    name: str = "clone_repo",
    args: dict[str, JsonValue] | None = None,
) -> tuple[CallRow, str]:
    token = f"token-{uuid4()}"
    step = await store.create_call(
        run.id,
        index,
        kind="step",
        name=name,
        function=f"fledge.steps:{name}",
        args={} if args is None else args,
        token_hash=hash_token(token),
    )
    return step, token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_launch_creates_manual_run(
    client: httpx.AsyncClient, store: Store
) -> None:
    response = await client.post(
        "/api/runs", json={"workflow": "ticket_to_pr", "ref": "7"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "manual"
    assert body["workflow"] == "ticket_to_pr"
    assert body["external_id"] == "7"
    assert body["status"] == "pending"
    run = await store.get_run(UUID(body["id"]))
    assert run is not None
    assert run.work == work_item("7")


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"workflow": "nope", "ref": "7"}, "unknown workflow 'nope'"),
        ({"workflow": "ticket_to_pr", "ref": "99"}, "no issue 99"),
    ],
)
async def test_launch_rejects_unknown_workflow_or_ref(
    client: httpx.AsyncClient, store: Store, payload: dict[str, str], detail: str
) -> None:
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert await store.list_runs() == []


async def test_list_runs_newest_first(client: httpx.AsyncClient, store: Store) -> None:
    first = await make_run(store)
    second = await make_run(store)
    response = await client.get("/api/runs")
    assert response.status_code == 200
    assert [run["id"] for run in response.json()] == [str(second.id), str(first.id)]


async def test_get_run_includes_steps_and_guidance(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    step, _ = await make_step(store, run)
    await store.finish_call(step.id, "succeeded", {"branch": "fledge/1"})
    await store.add_guidance(run.id, "web", "web", "first")
    await store.claim_guidance(run.id)
    await store.add_guidance(run.id, "trigger", "alice", "second", external_id="c1")

    response = await client.get(f"/api/runs/{run.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == str(run.id)
    [step_out] = body["calls"]
    assert step_out["name"] == "clone_repo"
    assert step_out["kind"] == "step"
    assert step_out["status"] == "succeeded"
    assert step_out["result"] == {"branch": "fledge/1"}
    assert "token_hash" not in step_out
    assert [(g["body"], g["delivered"]) for g in body["guidance"]] == [
        ("first", True),
        ("second", False),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/{id}",
        "/api/runs/{id}/events",
        "/runs/{id}",
        "/runs/{id}/live",
    ],
)
async def test_unknown_run_is_404(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path.format(id=uuid4()))
    assert response.status_code == 404


async def test_events_after_cursor(client: httpx.AsyncClient, store: Store) -> None:
    run = await make_run(store)
    first, second, third = await store.append_events(
        run.id, [("state", "a"), ("log", "b"), ("agent", "c")]
    )

    response = await client.get(f"/api/runs/{run.id}/events")
    assert [e["message"] for e in response.json()] == ["a", "b", "c"]

    response = await client.get(f"/api/runs/{run.id}/events?after={first.id}")
    assert response.status_code == 200
    assert [(e["id"], e["kind"]) for e in response.json()] == [
        (second.id, "log"),
        (third.id, "agent"),
    ]


async def test_cancel_active_run_then_finished_run_conflicts(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    response = await client.post(f"/api/runs/{run.id}/cancel")
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True

    await store.finish_run(run.id, "cancelled")
    response = await client.post(f"/api/runs/{run.id}/cancel")
    assert response.status_code == 409

    response = await client.post(f"/api/runs/{uuid4()}/cancel")
    assert response.status_code == 404


async def test_add_guidance(client: httpx.AsyncClient, store: Store) -> None:
    run = await make_run(store)
    response = await client.post(
        f"/api/runs/{run.id}/guidance", json={"body": "use tabs"}
    )
    assert response.status_code == 201
    assert response.json()["delivered"] is False
    [guidance] = await store.list_guidance(run.id)
    assert (guidance.source, guidance.author, guidance.body) == (
        "web",
        "web",
        "use tabs",
    )


@pytest.mark.parametrize("body", ["", "   "])
async def test_empty_guidance_is_rejected(
    client: httpx.AsyncClient, store: Store, body: str
) -> None:
    run = await make_run(store)
    response = await client.post(f"/api/runs/{run.id}/guidance", json={"body": body})
    assert response.status_code == 422
    assert await store.list_guidance(run.id) == []


async def test_guidance_for_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/api/runs/{uuid4()}/guidance", json={"body": "x"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "route",
    [
        ("GET", "", None),
        ("POST", "/events", {"events": []}),
        ("POST", "/guidance/claim", None),
        ("POST", "/result", {"status": "succeeded"}),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [{}, bearer("wrong"), {"Authorization": "Basic abc"}],
    ids=["missing", "wrong", "not-bearer"],
)
async def test_internal_routes_require_the_step_token(
    client: httpx.AsyncClient,
    store: Store,
    route: tuple[str, str, object],
    headers: dict[str, str],
) -> None:
    method, suffix, payload = route
    run = await make_run(store)
    step, _ = await make_step(store, run)
    await store.add_guidance(run.id, "web", "web", "pending")
    response = await client.request(
        method, f"/internal/step-runs/{step.id}{suffix}", json=payload, headers=headers
    )
    assert response.status_code == 401
    refreshed = await store.get_call(step.id)
    assert refreshed is not None
    assert refreshed.status == "running"
    [guidance] = await store.list_guidance(run.id)
    assert guidance.delivered_at is None


async def test_a_wait_has_no_token_to_authenticate_with(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    wait = await store.create_call(
        run.id, 0, "wait", "merged", function="fledge.github.pulls:merged", args={}
    )
    assert wait.token_hash is None

    response = await client.get(
        f"/internal/step-runs/{wait.id}", headers=bearer("anything")
    )

    assert response.status_code == 401


async def test_internal_unknown_step_run_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/internal/step-runs/{uuid4()}", headers=bearer("x"))
    assert response.status_code == 404


async def test_internal_context_has_the_calls_function_and_arguments(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    clone, _ = await make_step(store, run, 0, "clone_repo")
    await store.finish_call(clone.id, "succeeded", {"branch": "fledge/1"})
    implement, token = await make_step(
        store, run, 1, "run_turn", {"agent": "implementer", "prompt": "Fix it"}
    )

    response = await client.get(
        f"/internal/step-runs/{implement.id}", headers=bearer(token)
    )
    assert response.status_code == 200
    assert response.json() == {
        "run_id": str(run.id),
        "work": {
            "external_id": "1",
            "title": "Fix the thing",
            "body": "Body",
            "url": "https://example.test/issues/1",
            "repo_url": "https://example.test/repo.git",
        },
        "function": "fledge.steps:run_turn",
        "args": {"agent": "implementer", "prompt": "Fix it"},
        "workflow_files": SNAPSHOT_FILES,
    }


async def test_internal_events_are_stored(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    step, token = await make_step(store, run)
    response = await client.post(
        f"/internal/step-runs/{step.id}/events",
        json={
            "events": [
                {"kind": "log", "message": "cloning"},
                {"kind": "agent", "message": "thinking"},
            ]
        },
        headers=bearer(token),
    )
    assert response.status_code == 204
    events = await store.list_events(run.id)
    assert [(e.kind, e.message, e.call_id) for e in events] == [
        ("log", "cloning", step.id),
        ("agent", "thinking", step.id),
    ]


async def test_internal_guidance_is_claimed_once(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    step, token = await make_step(store, run)
    await store.add_guidance(run.id, "web", "web", "first")
    await store.add_guidance(run.id, "web", "web", "second")
    url = f"/internal/step-runs/{step.id}/guidance/claim"

    response = await client.post(url, headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {"bodies": ["first", "second"]}

    response = await client.post(url, headers=bearer(token))
    assert response.json() == {"bodies": []}


@pytest.mark.parametrize(
    ("result", "event"),
    [
        (
            {"status": "succeeded", "result": {"branch": "fledge/1"}},
            "step clone_repo succeeded",
        ),
        (
            {"status": "failed", "error": "RuntimeError: clone failed"},
            "step clone_repo failed: RuntimeError: clone failed",
        ),
    ],
    ids=["succeeded", "failed"],
)
async def test_internal_result_is_recorded_once_with_a_state_event(
    client: httpx.AsyncClient,
    store: Store,
    result: dict[str, JsonValue],
    event: str,
) -> None:
    run = await make_run(store)
    step, token = await make_step(store, run)
    url = f"/internal/step-runs/{step.id}/result"

    response = await client.post(url, json=result, headers=bearer(token))
    assert response.status_code == 204
    finished = await store.get_call(step.id)
    assert finished is not None
    assert (finished.status, finished.result, finished.error) == (
        result["status"],
        result.get("result"),
        result.get("error"),
    )
    events = await store.list_events(run.id)
    assert [(e.kind, e.message, e.call_id) for e in events] == [
        ("state", event, step.id)
    ]

    response = await client.post(
        url, json={"status": "succeeded", "result": "late"}, headers=bearer(token)
    )
    assert response.status_code == 409
    unchanged = await store.get_call(step.id)
    assert unchanged == finished
    assert len(await store.list_events(run.id)) == 1


async def test_index_page_lists_runs_escaped(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store, UNTRUSTED_TITLE)
    response = await client.get("/")
    assert response.status_code == 200
    html = response.text
    assert f'href="/runs/{run.id}"' in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert UNTRUSTED_TITLE not in html
    assert '<option value="ticket_to_pr">ticket_to_pr</option>' in html


async def test_run_page_renders_calls_and_guidance(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store, UNTRUSTED_TITLE)
    results: list[JsonValue] = [
        "https://example.test/pull/3",
        "javascript:x",
        {"branch": "<b>main</b>"},
        [1, "two"],
        None,
    ]
    for index, result in enumerate(results):
        step, _ = await make_step(store, run, index, f"call_{index}")
        await store.finish_call(step.id, "succeeded", result)
    await store.add_guidance(run.id, "web", "web", "<b>bold</b>")

    response = await client.get(f"/runs/{run.id}")
    assert response.status_code == 200
    html = response.text
    assert UNTRUSTED_TITLE not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<h2>Calls</h2>" in html
    assert (
        "<th>Call</th><th>Kind</th><th>Status</th><th>Result</th><th>Error</th>" in html
    )
    assert '<a href="https://example.test/issues/1">' in html
    assert '<a href="https://example.test/pull/3">' in html
    assert 'href="javascript:x"' not in html
    assert "<td>javascript:x</td>" in html
    assert "<td>{&#34;branch&#34;:&#34;&lt;b&gt;main&lt;/b&gt;&#34;}</td>" in html
    assert "<td>[1,&#34;two&#34;]</td>" in html
    assert "<td>None</td>" not in html
    assert "<td>step</td>" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "not delivered" in html
    assert 'id="cancel" type="button">' in html


async def test_run_page_hides_cancel_for_finished_run(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    await store.finish_run(run.id, "succeeded")
    response = await client.get(f"/runs/{run.id}")
    assert 'id="cancel" type="button" hidden>' in response.text


async def test_launch_form_redirects_to_the_new_run(
    client: httpx.AsyncClient, store: Store
) -> None:
    response = await client.post("/runs", data={"workflow": "ticket_to_pr", "ref": "7"})
    assert response.status_code == 201
    [run] = await store.list_runs()
    assert response.headers["HX-Redirect"] == f"/runs/{run.id}"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"workflow": "ticket_to_pr", "ref": "99"}, "no issue 99"),
        ({"workflow": "nope", "ref": "7"}, "unknown workflow 'nope'"),
        ({"workflow": "ticket_to_pr"}, "ref: Field required"),
    ],
)
async def test_launch_form_error_is_html_to_show(
    client: httpx.AsyncClient, store: Store, form: dict[str, str], message: str
) -> None:
    response = await client.post("/runs", data=form)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == message
    assert await store.list_runs() == []


async def test_cancel_button_requests_cancel_then_reports_finished(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    response = await client.post(f"/runs/{run.id}/cancel")
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "refresh"
    refreshed = await store.get_run(run.id)
    assert refreshed is not None
    assert refreshed.cancel_requested

    await store.finish_run(run.id, "cancelled")
    response = await client.post(f"/runs/{run.id}/cancel")
    assert response.status_code == 409
    assert response.text == "run has already finished"


async def test_guidance_form_adds_guidance_and_rejects_blank(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    response = await client.post(f"/runs/{run.id}/guidance", data={"body": "   "})
    assert response.status_code == 422
    assert "body:" in response.text
    assert await store.list_guidance(run.id) == []

    response = await client.post(f"/runs/{run.id}/guidance", data={"body": "use tabs"})
    assert response.status_code == 201
    assert response.headers["HX-Trigger"] == "refresh"
    [guidance] = await store.list_guidance(run.id)
    assert guidance.body == "use tabs"


async def test_live_fragment_appends_new_events_and_polls_while_active(
    client: httpx.AsyncClient, store: Store
) -> None:
    run = await make_run(store)
    first, second = await store.append_events(run.id, [("log", "a"), ("log", "<b>")])

    page = (await client.get(f"/runs/{run.id}")).text
    assert f'hx-get="/runs/{run.id}/live?after={second.id}"' in page
    assert "[log] a</div>" in page

    response = await client.get(f"/runs/{run.id}/live?after={first.id}")
    assert response.status_code == 200
    live = response.text
    assert "every 2s" in live
    assert f"/live?after={second.id}" in live
    assert '<div hx-swap-oob="beforeend:#events"><div>' in live
    assert "[log] &lt;b&gt;</div>" in live
    assert "[log] a</div>" not in live
    assert '<table id="calls" hx-swap-oob="true">' in live

    await store.finish_run(run.id, "succeeded")
    live = (await client.get(f"/runs/{run.id}/live?after={second.id}")).text
    assert "every 2s" not in live
    assert 'hx-trigger="refresh from:body"' in live
