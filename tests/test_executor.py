import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue

from fledge.executor import ControlPlane, ReadyPolicy, RetryPolicy, execute
from fledge.wire import READY_PORTS_ENV
from fledge.workflows import snapshot_hash

STEP_RUN_ID = uuid4()
RUN_ID = uuid4()
CREDENTIAL = "step-run-credential"
BASE = f"/internal/step-runs/{STEP_RUN_ID}"
FAST = RetryPolicy(window=1.0, interval=0.001)
FAST_READY = ReadyPolicy(timeout=1.0, interval=0.01)
WORKSPACE = Path("/workspace-under-test")

STEPS_SOURCE = """\
from dataclasses import dataclass

from fledge import Agent, Repo, Run, StepContext, step, workflow

from .shared import FakeHarness, NoTrigger

helper = Agent("helper", FakeHarness())


@dataclass(frozen=True)
class Summary:
    branch: str
    path: str
    title: str
    note: str
    guidance: list[str]
    run_id: str
    workspace: str


@step
def summarize(ctx: StepContext, repo: Repo, note: str) -> Summary:
    ctx.log("hello")
    ctx.agent_event("thinking")
    return Summary(
        branch=repo.branch,
        path=str(repo.path),
        title=ctx.work.title,
        note=note,
        guidance=ctx.claim_guidance(),
        run_id=str(ctx.run_id),
        workspace=str(ctx.workspace),
    )


@step
def explode(ctx: StepContext) -> None:
    raise ValueError("nothing works")


@step
def harness_of(ctx: StepContext, agent: str) -> str:
    return type(ctx.harness(agent)).__name__


@workflow(trigger=NoTrigger())
def tickets(run: Run) -> None:
    explode()
"""
SHARED_SOURCE = """\
class NoTrigger:
    def start(self):
        raise AssertionError("step containers never start triggers")


class FakeHarness:
    def run_turn(self, turn, on_event):
        raise AssertionError("not prompted in these tests")
"""
REPO: dict[str, JsonValue] = {
    "url": "https://example.com/repo",
    "path": "/workspace-under-test/repo",
    "branch": "fledge/7",
    "base": "main",
}


def context(function: str, args: dict[str, JsonValue]) -> dict[str, object]:
    """A context for a step in a snapshot this process has not loaded yet.

    A fresh snapshot makes the executor do the import itself, as it does in a
    new container.
    """
    files = {
        "tickets.py": STEPS_SOURCE,
        "shared.py": SHARED_SOURCE,
        "unique.py": f"# {uuid4()}\n",
    }
    package = f"fledge_workflows_{snapshot_hash(files)[:16]}"
    return {
        "run_id": str(RUN_ID),
        "work": {
            "external_id": "7",
            "title": "Fix it",
            "body": "It is broken",
            "url": "https://example.com/issues/7",
            "repo_url": "https://example.com/repo",
        },
        "function": f"{package}.tickets:{function}",
        "args": args,
        "workflow_files": files,
    }


def summarize_context() -> dict[str, object]:
    return context("summarize", {"repo": REPO, "note": "be brief"})


@dataclass
class FakeControlPlane:
    """Records requests; `failures` responses are returned before normal ones."""

    context: dict[str, object] = field(default_factory=summarize_context)
    guidance: list[str] = field(default_factory=list)
    failures: list[Callable[[], httpx.Response]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            return self.failures.pop(0)()
        path = request.url.path
        if request.method == "GET" and path == BASE:
            return httpx.Response(200, json=self.context)
        if request.method == "POST" and path == f"{BASE}/guidance/claim":
            bodies, self.guidance = self.guidance, []
            return httpx.Response(200, json={"bodies": bodies})
        if request.method == "POST" and path in {f"{BASE}/events", f"{BASE}/result"}:
            return httpx.Response(204)
        return httpx.Response(404)

    def bodies(self, suffix: str) -> list[dict[str, object]]:
        return [
            json.loads(r.content) for r in self.requests if r.url.path == BASE + suffix
        ]


def run_executor(
    fake: FakeControlPlane,
    retry: RetryPolicy = FAST,
    ready: ReadyPolicy = FAST_READY,
) -> int:
    client = httpx.Client(
        base_url="http://control-plane",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        transport=httpx.MockTransport(fake.handle),
    )
    return execute(ControlPlane(client, STEP_RUN_ID, retry), WORKSPACE, ready)


def test_success_reports_the_typed_result_and_sends_token_on_every_call() -> None:
    fake = FakeControlPlane(guidance=["use tabs", "add a test"])

    assert run_executor(fake) == 0

    assert fake.bodies("/events") == [
        {"events": [{"kind": "log", "message": "hello"}]},
        {"events": [{"kind": "agent", "message": "thinking"}]},
    ]
    assert fake.bodies("/result") == [
        {
            "status": "succeeded",
            "result": {
                "branch": "fledge/7",
                "path": "/workspace-under-test/repo",
                "title": "Fix it",
                "note": "be brief",
                "guidance": ["use tabs", "add a test"],
                "run_id": str(RUN_ID),
                "workspace": "/workspace-under-test",
            },
            "error": None,
        }
    ]
    assert [r.url.path for r in fake.requests] == [
        BASE,
        f"{BASE}/events",
        f"{BASE}/events",
        f"{BASE}/guidance/claim",
        f"{BASE}/result",
    ]
    assert {r.headers["Authorization"] for r in fake.requests} == {
        f"Bearer {CREDENTIAL}"
    }


@pytest.mark.parametrize(
    ("step_context", "error"),
    [
        (context("explode", {}), "ValueError: nothing works"),
        (
            context("harness_of", {"agent": "nobody"}),
            "LookupError: the run's workflow code defines no agent 'nobody'",
        ),
        (context("tickets", {}), "TypeError: "),
    ],
    ids=["step-raises", "unknown-agent", "not-a-step"],
)
def test_failures_are_reported_as_failed(
    step_context: dict[str, object], error: str
) -> None:
    fake = FakeControlPlane(context=step_context)

    assert run_executor(fake) == 1

    [result] = fake.bodies("/result")
    assert result["status"] == "failed"
    assert result["result"] is None
    assert str(result["error"]).startswith(error)


def test_a_step_finds_the_runs_agents_by_name() -> None:
    fake = FakeControlPlane(context=context("harness_of", {"agent": "helper"}))

    assert run_executor(fake) == 0

    [result] = fake.bodies("/result")
    assert result["result"] == "FakeHarness"


@pytest.fixture
def listening_port() -> Iterator[int]:
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield server.getsockname()[1]


@pytest.fixture
def closed_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port: int = server.getsockname()[1]
    return port


def test_waits_for_services_that_accept_connections(
    monkeypatch: pytest.MonkeyPatch, listening_port: int
) -> None:
    monkeypatch.setenv(READY_PORTS_ENV, f"127.0.0.1:{listening_port}")
    fake = FakeControlPlane()

    assert run_executor(fake) == 0

    assert fake.bodies("/result")[0]["status"] == "succeeded"


def test_a_service_that_never_accepts_connections_fails_the_step(
    monkeypatch: pytest.MonkeyPatch, listening_port: int, closed_port: int
) -> None:
    monkeypatch.setenv(
        READY_PORTS_ENV, f"127.0.0.1:{listening_port},127.0.0.1:{closed_port}"
    )
    fake = FakeControlPlane()

    assert run_executor(fake, ready=ReadyPolicy(timeout=0.05, interval=0.01)) == 1

    assert fake.bodies("/events") == []
    assert fake.bodies("/result") == [
        {
            "status": "failed",
            "result": None,
            "error": f"TimeoutError: service 127.0.0.1:{closed_port} did not "
            "accept a connection within 0.05 seconds",
        }
    ]


def refuse_connection() -> httpx.Response:
    raise httpx.ConnectError("connection refused")


@pytest.mark.parametrize(
    "failure",
    [refuse_connection, lambda: httpx.Response(503)],
    ids=["connect-error", "server-error"],
)
def test_transient_failures_are_retried(
    failure: Callable[[], httpx.Response],
) -> None:
    fake = FakeControlPlane(failures=[failure, failure])

    assert run_executor(fake) == 0

    assert [r.url.path for r in fake.requests[:3]] == [BASE, BASE, BASE]
    assert fake.bodies("/result")[0]["status"] == "succeeded"


def test_gives_up_on_connection_failures_after_the_window() -> None:
    fake = FakeControlPlane(failures=[refuse_connection] * 1000)

    with pytest.raises(httpx.ConnectError):
        run_executor(fake, RetryPolicy(window=0.02, interval=0.001))

    assert 1 < len(fake.requests) < 1000


def test_gives_up_on_server_errors_after_the_window() -> None:
    fake = FakeControlPlane(failures=[lambda: httpx.Response(500)] * 1000)

    with pytest.raises(httpx.HTTPStatusError):
        run_executor(fake, RetryPolicy(window=0.02, interval=0.001))


def test_client_errors_are_not_retried() -> None:
    fake = FakeControlPlane(failures=[lambda: httpx.Response(401)])

    with pytest.raises(httpx.HTTPStatusError):
        run_executor(fake)

    assert len(fake.requests) == 1
