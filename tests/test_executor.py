import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from fledge.core import Step, StepContext, Trigger, Workflow
from fledge.executor import ControlPlane, RetryPolicy, execute

STEP_RUN_ID = uuid4()
RUN_ID = uuid4()
CREDENTIAL = "step-run-credential"
BASE = f"/internal/step-runs/{STEP_RUN_ID}"
FAST = RetryPolicy(window=1.0, interval=0.001)

CONTEXT = {
    "run_id": str(RUN_ID),
    "workflow": "fake",
    "step_index": 1,
    "work": {
        "external_id": "7",
        "title": "Fix it",
        "body": "It is broken",
        "url": "https://example.com/issues/7",
        "repo_url": "https://example.com/repo",
    },
    "outputs": {"first": {"summary": "earlier"}},
    "workflow_files": {},
}


@dataclass
class FakeControlPlane:
    """Records requests; `failures` responses are returned before normal ones."""

    guidance: list[str] = field(default_factory=list)
    context: dict[str, object] = field(default_factory=lambda: CONTEXT)
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


@dataclass(frozen=True)
class NamedStep:
    name: str

    def run(self, ctx: StepContext) -> dict[str, str]:
        raise AssertionError("only the step at step_index should run")


@dataclass(frozen=True)
class TalkingStep:
    name: str = "talk"

    def run(self, ctx: StepContext) -> dict[str, str]:
        ctx.log("hello")
        ctx.agent_event("thinking")
        guidance = ctx.claim_guidance()
        return {
            "guidance": "|".join(guidance),
            "title": ctx.work.title,
            "earlier": ctx.outputs["first"]["summary"],
            "run_id": str(ctx.run_id),
            "workspace": str(ctx.workspace),
        }


@dataclass(frozen=True)
class FailingStep:
    name: str = "fail"

    def run(self, ctx: StepContext) -> dict[str, str]:
        raise ValueError("nothing works")


class NoTrigger:
    def start(self) -> Trigger:
        raise AssertionError("step containers never start triggers")


def control_plane(fake: FakeControlPlane, retry: RetryPolicy = FAST) -> ControlPlane:
    client = httpx.Client(
        base_url="http://control-plane",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        transport=httpx.MockTransport(fake.handle),
    )
    return ControlPlane(client, STEP_RUN_ID, retry)


def run_executor(fake: FakeControlPlane, step: Step, retry: RetryPolicy = FAST) -> int:
    workflow = Workflow(
        name="fake", trigger=NoTrigger(), steps=[NamedStep("first"), step]
    )
    return execute(
        control_plane(fake, retry),
        load=lambda files: {"fake": workflow},
        workspace=Path("/workspace-under-test"),
    )


WORKFLOW_SOURCE = """\
from fledge.core import Workflow

from .shared import Echo


class NoTrigger:
    def start(self):
        raise AssertionError("step containers never start triggers")


fake = Workflow(name="fake", trigger=NoTrigger(), steps=[Echo("first"), Echo("echo")])
"""
SHARED_SOURCE = """\
class Echo:
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return {"step": self.name, "title": ctx.work.title}
"""


def test_runs_the_step_from_the_runs_workflow_files() -> None:
    fake = FakeControlPlane(
        context=CONTEXT
        | {"workflow_files": {"fake.py": WORKFLOW_SOURCE, "shared.py": SHARED_SOURCE}}
    )

    assert execute(control_plane(fake), workspace=Path("/workspace-under-test")) == 0

    [result] = fake.bodies("/result")
    assert result["output"] == {"step": "echo", "title": "Fix it"}


def test_success_reports_outputs_and_sends_token_on_every_call() -> None:
    fake = FakeControlPlane(guidance=["use tabs", "add a test"])

    assert run_executor(fake, TalkingStep()) == 0

    assert fake.bodies("/events") == [
        {"events": [{"kind": "log", "message": "hello"}]},
        {"events": [{"kind": "agent", "message": "thinking"}]},
    ]
    assert fake.bodies("/result") == [
        {
            "status": "succeeded",
            "output": {
                "guidance": "use tabs|add a test",
                "title": "Fix it",
                "earlier": "earlier",
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


def test_step_exception_is_reported_as_failed() -> None:
    fake = FakeControlPlane()

    assert run_executor(fake, FailingStep()) == 1

    assert fake.bodies("/result") == [
        {"status": "failed", "output": {}, "error": "ValueError: nothing works"}
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

    assert run_executor(fake, TalkingStep()) == 0

    assert [r.url.path for r in fake.requests[:3]] == [BASE, BASE, BASE]
    assert fake.bodies("/result")[0]["status"] == "succeeded"


def test_gives_up_on_connection_failures_after_the_window() -> None:
    fake = FakeControlPlane(failures=[refuse_connection] * 1000)

    with pytest.raises(httpx.ConnectError):
        run_executor(fake, TalkingStep(), RetryPolicy(window=0.02, interval=0.001))

    assert 1 < len(fake.requests) < 1000


def test_gives_up_on_server_errors_after_the_window() -> None:
    fake = FakeControlPlane(failures=[lambda: httpx.Response(500)] * 1000)

    with pytest.raises(httpx.HTTPStatusError):
        run_executor(fake, TalkingStep(), RetryPolicy(window=0.02, interval=0.001))


def test_client_errors_are_not_retried() -> None:
    fake = FakeControlPlane(failures=[lambda: httpx.Response(401)])

    with pytest.raises(httpx.HTTPStatusError):
        run_executor(fake, TalkingStep())

    assert len(fake.requests) == 1
