from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from pydantic import JsonValue

from fledge import core
from fledge.checks import (
    Passed,
    Pending,
    Stopped,
    Verdict,
    WaitContext,
    render_feedback,
    until,
    wait,
)
from fledge.core import Sandbox, Service, StepContext, Trigger, WorkItem
from fledge.engine import StepFailed, ref, step, workflow
from fledge.orchestrator import Orchestrator
from fledge.runner.base import ContainerState, StepSpec
from fledge.settings import Settings
from fledge.store import Run, StepRun, Store, WorkflowRef, hash_token
from fledge.wire import API_URL_ENV, READY_PORTS_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV

pytestmark = pytest.mark.anyio


SNAPSHOT_HASH = "snapshot-under-test"


class _NoTrigger:
    def start(self) -> Trigger:
        raise AssertionError("not started in these tests")


@step
def make_branch(ctx: StepContext, issue: str) -> str:
    raise AssertionError("steps run in a container, not in the control plane")


@step
def open_pr(ctx: StepContext, branch: str) -> str:
    raise AssertionError("steps run in a container, not in the control plane")


@step
def fix(ctx: StepContext, findings: str) -> None:
    raise AssertionError("steps run in a container, not in the control plane")


DB = Service("db", "postgres:16", env={"POSTGRES_PASSWORD": "x"}, ready_port=5432)
CACHE = Service("cache", "redis:7")
SEARCH = Service("search", "opensearch:2", ready_port=9200)


@step(sandbox=Sandbox(services=(DB, CACHE, SEARCH), env={"MODE": "test"}))
def integration_tests(ctx: StepContext) -> None:
    raise AssertionError("steps run in a container, not in the control plane")


VERDICTS: list[Verdict] = []
"""What the ci wait returns on its next polls; Pending once empty."""


POLL_ERRORS: list[Exception] = []


@wait
def ci(ctx: WaitContext) -> Verdict:
    if POLL_ERRORS:
        raise POLL_ERRORS.pop(0)
    return VERDICTS.pop(0) if VERDICTS else Pending()


@workflow(trigger=_NoTrigger())
def two_steps(run: core.Run) -> None:
    branch = make_branch(run.work.external_id)
    open_pr(branch)


@workflow(trigger=_NoTrigger())
def tolerant(run: core.Run) -> None:
    try:
        make_branch(run.work.external_id)
    except StepFailed:
        open_pr("fallback")


@workflow(trigger=_NoTrigger())
def until_green(run: core.Run) -> None:
    for feedback in until(ci(), max_rounds=3):
        fix(render_feedback(feedback))


@workflow(trigger=_NoTrigger())
def sandboxed(run: core.Run) -> None:
    integration_tests()


WORKFLOWS = {wf.name: wf for wf in [two_steps, tolerant, until_green, sandboxed]}


async def load_workflow(snapshot_hash: str, name: str) -> core.Workflow:
    assert snapshot_hash == SNAPSHOT_HASH
    return WORKFLOWS[name]


@pytest.fixture(autouse=True)
def _no_leftover_verdicts() -> Iterator[None]:
    VERDICTS.clear()
    yield
    VERDICTS.clear()


@dataclass
class FakeRunner:
    workspaces: set[UUID] = field(default_factory=set)
    deleted_workspaces: set[UUID] = field(default_factory=set)
    failing_workspaces: set[UUID] = field(default_factory=set)
    started: list[StepSpec] = field(default_factory=list)
    stopped: set[UUID] = field(default_factory=set)
    states: dict[UUID, ContainerState] = field(default_factory=dict)
    before_state: Callable[[UUID], Awaitable[None]] | None = None

    async def create_workspace(self, run_id: UUID) -> None:
        if run_id in self.failing_workspaces:
            raise RuntimeError("volume could not be created")
        self.workspaces.add(run_id)

    async def start_step(self, spec: StepSpec) -> None:
        assert spec.run_id in self.workspaces
        self.started.append(spec)
        self.states[spec.step_run_id] = ContainerState.RUNNING

    async def step_state(self, step_run_id: UUID) -> ContainerState:
        if self.before_state is not None:
            await self.before_state(step_run_id)
        return self.states.get(step_run_id, ContainerState.MISSING)

    async def stop_step(self, step_run_id: UUID) -> None:
        self.stopped.add(step_run_id)
        self.states.pop(step_run_id, None)

    async def delete_workspace(self, run_id: UUID) -> None:
        self.deleted_workspaces.add(run_id)


def make_config(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "image": "fledge:test",
            "api_url": "http://fledge:8000",
            "docker_network": "fledge",
            "max_concurrent_steps": 5,
            "poll_interval_seconds": 0,
            **overrides,
        }
    )


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with Store(database_url) as store:
        await store.save_snapshot(SNAPSHOT_HASH, {})
        yield store


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def orchestrator(store: Store, runner: FakeRunner, **config: object) -> Orchestrator:
    return Orchestrator(store, runner, make_config(**config), load_workflow)


async def make_run(
    store: Store, workflow: core.Workflow, external_id: str = "1"
) -> Run:
    work = WorkItem(
        external_id=external_id,
        title="Fix it",
        body="",
        url=f"https://example.test/{external_id}",
        repo_url="https://example.test/repo.git",
    )
    return await store.create_manual_run(
        WorkflowRef(workflow.name, SNAPSHOT_HASH), work
    )


async def get_run(store: Store, run: Run) -> Run:
    current = await store.get_run(run.id)
    assert current is not None
    return current


async def journal(store: Store, run: Run) -> list[StepRun]:
    return await store.list_step_runs(run.id)


async def latest_call(store: Store, run: Run) -> StepRun:
    return (await journal(store, run))[-1]


async def report(
    store: Store,
    runner: FakeRunner,
    step_run: StepRun,
    result: JsonValue = None,
    *,
    error: str | None = None,
) -> None:
    """Post a result as the internal API would, then let the container exit."""
    if error is None:
        assert await store.finish_step_run(step_run.id, "succeeded", result)
    else:
        assert await store.finish_step_run(step_run.id, "failed", error=error)
    runner.states[step_run.id] = ContainerState.EXITED


async def timeline(store: Store, run: Run) -> list[str]:
    return [event.message for event in await store.list_events(run.id)]


async def test_run_passes_results_between_steps_to_success(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, two_steps, "42")

    await orch.tick()
    assert (await get_run(store, run)).status == "running"
    assert runner.workspaces == {run.id}
    first = await latest_call(store, run)
    assert (first.step_index, first.kind, first.name) == (0, "step", "make_branch")
    assert first.function == ref(make_branch.fn)
    assert first.args == {"issue": "42"}

    await orch.tick()
    assert len(runner.started) == 1

    await report(store, runner, first, "fledge/42")
    await orch.tick()
    second = await latest_call(store, run)
    assert (second.step_index, second.name, second.args) == (
        1,
        "open_pr",
        {"branch": "fledge/42"},
    )
    assert first.id in runner.stopped
    assert (await get_run(store, run)).status == "running"

    await report(store, runner, second, "https://example.test/pr/1")
    await orch.tick()
    finished = await get_run(store, run)
    assert (finished.status, finished.error) == ("succeeded", None)
    assert second.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert [s.step_run_id for s in runner.started] == [first.id, second.id]
    assert await timeline(store, run) == [
        "run started",
        "step make_branch started",
        "step open_pr started",
        "run succeeded",
    ]

    await orch.tick()
    assert len(runner.started) == 2


async def test_failed_step_fails_the_run_and_cleans_up(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, two_steps)
    await orch.tick()
    step_run = await latest_call(store, run)

    await report(store, runner, step_run, error="boom")
    await orch.tick()

    failed = await get_run(store, run)
    assert (failed.status, failed.error) == ("failed", "StepFailed: boom")
    assert step_run.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert len(await journal(store, run)) == 1
    assert (await timeline(store, run))[-1] == "run failed: StepFailed: boom"


async def test_workflow_that_catches_a_failed_step_carries_on(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, tolerant)
    await orch.tick()
    await report(store, runner, await latest_call(store, run), error="boom")

    await orch.tick()

    assert (await get_run(store, run)).status == "running"
    fallback = await latest_call(store, run)
    assert (fallback.name, fallback.args) == ("open_pr", {"branch": "fallback"})

    await report(store, runner, fallback, "url")
    await orch.tick()
    assert (await get_run(store, run)).status == "succeeded"


@pytest.mark.parametrize(
    ("timeout_minutes", "state", "error"),
    [
        (0, ContainerState.RUNNING, "step timed out after 0 minutes"),
        (60, ContainerState.EXITED, "step container exited without reporting a result"),
        (
            60,
            ContainerState.MISSING,
            "step container exited without reporting a result",
        ),
    ],
)
async def test_step_that_times_out_or_loses_its_container_fails(
    store: Store,
    runner: FakeRunner,
    timeout_minutes: int,
    state: ContainerState,
    error: str,
) -> None:
    orch = orchestrator(store, runner, step_timeout_minutes=timeout_minutes)
    run = await make_run(store, two_steps)
    await orch.tick()
    step_run = await latest_call(store, run)
    runner.states[step_run.id] = state

    await orch.tick()

    current = await store.get_step_run(step_run.id)
    assert current is not None
    assert (current.status, current.error) == ("failed", error)
    assert runner.states.get(step_run.id) is not ContainerState.RUNNING
    # The workflow gets a chance to catch the failure on the next replay.
    assert (await get_run(store, run)).status == "running"
    assert (await timeline(store, run))[-1] == f"step make_branch failed: {error}"

    await orch.tick()

    failed = await get_run(store, run)
    assert (failed.status, failed.error) == ("failed", f"StepFailed: {error}")
    assert runner.deleted_workspaces == {run.id}


async def test_step_within_its_timeout_keeps_running(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, step_timeout_minutes=60)
    run = await make_run(store, two_steps)
    await orch.tick()
    await orch.tick()
    assert (await latest_call(store, run)).status == "running"
    assert runner.stopped == set()


async def test_result_posted_just_before_exit_is_honoured(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, two_steps)
    await orch.tick()
    step_run = await latest_call(store, run)

    async def post_then_exit(step_run_id: UUID) -> None:
        # The result lands after the tick listed the step runs as running.
        assert await store.finish_step_run(step_run_id, "succeeded", "fledge/1")
        runner.states[step_run_id] = ContainerState.EXITED

    runner.before_state = post_then_exit
    await orch.tick()

    current = await store.get_step_run(step_run.id)
    assert current is not None
    assert (current.status, current.result) == ("succeeded", "fledge/1")

    runner.before_state = None
    await orch.tick()
    assert (await latest_call(store, run)).name == "open_pr"


async def test_cancel_while_pending(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, two_steps)
    assert await store.request_cancel(run.id)

    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    assert runner.workspaces == set()
    assert runner.started == []
    assert await timeline(store, run) == ["run cancelled"]


async def test_cancel_while_a_step_runs(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, two_steps)
    await orch.tick()
    step_run = await latest_call(store, run)
    assert await store.request_cancel(run.id)

    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    assert (await latest_call(store, run)).status == "cancelled"
    assert step_run.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert (await timeline(store, run))[-2:] == [
        "step make_branch cancelled",
        "run cancelled",
    ]


async def test_cancel_while_waiting(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, until_green)
    await orch.tick()
    waiting = await latest_call(store, run)
    assert (waiting.kind, waiting.status) == ("wait", "running")
    assert await store.request_cancel(run.id)

    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    assert (await latest_call(store, run)).status == "cancelled"
    assert runner.stopped == set()
    assert runner.deleted_workspaces == {run.id}
    assert (await timeline(store, run))[-2:] == ["ci cancelled", "run cancelled"]


async def test_step_limit_defers_steps_but_not_waits(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, max_concurrent_steps=1)
    first = await make_run(store, two_steps, "1")
    second = await make_run(store, two_steps, "2")
    waiting = await make_run(store, until_green, "3")

    await orch.tick()

    statuses = {(await get_run(store, run)).status for run in [first, second, waiting]}
    assert statuses == {"running"}
    assert len(await journal(store, first)) == 1
    assert await journal(store, second) == []
    assert (await latest_call(store, waiting)).kind == "wait"
    assert await store.count_running_steps() == 1

    await orch.tick()
    assert await journal(store, second) == []

    await report(store, runner, await latest_call(store, first), "fledge/1")
    await orch.tick()
    # The oldest run takes the freed slot for its next step.
    assert len(await journal(store, first)) == 2
    assert await journal(store, second) == []

    await report(store, runner, await latest_call(store, first), "url")
    await orch.tick()
    assert (await get_run(store, first)).status == "succeeded"
    assert len(await journal(store, second)) == 1


async def test_wait_is_polled_until_it_settles(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, until_green)

    await orch.tick()
    waiting = await latest_call(store, run)
    assert (waiting.step_index, waiting.name, waiting.status) == (0, "ci", "running")
    assert waiting.function == f"{__name__}:ci"

    VERDICTS.append(Passed())
    await orch.tick()
    passed = await latest_call(store, run)
    assert (passed.status, passed.result) == ("succeeded", {"kind": "passed"})

    await orch.tick()
    assert (await get_run(store, run)).status == "succeeded"
    assert runner.started == []
    assert await timeline(store, run) == [
        "run started",
        "waiting on ci",
        "ci finished",
        "run succeeded",
    ]


async def test_wait_whose_poll_raises_shows_on_the_timeline_and_is_retried(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, until_green)
    POLL_ERRORS.append(PermissionError("token cannot read checks"))

    await orch.tick()
    assert (await latest_call(store, run)).status == "running"
    assert await timeline(store, run) == [
        "run started",
        "waiting on ci",
        "ci could not be checked, will retry: "
        "PermissionError: token cannot read checks",
    ]

    VERDICTS.append(Passed())
    await orch.tick()
    assert (await latest_call(store, run)).status == "succeeded"


async def test_wait_polls_at_most_every_interval(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, poll_interval_seconds=3600)
    run = await make_run(store, until_green)
    VERDICTS.extend([Pending(), Passed()])

    await orch.tick()
    await orch.tick()
    assert [Passed()] == VERDICTS

    # A restarted control plane does not know when it last polled.
    await orchestrator(store, runner, poll_interval_seconds=3600).tick()
    assert VERDICTS == []
    assert (await latest_call(store, run)).status == "succeeded"


async def test_guidance_during_a_wait_becomes_feedback(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, until_green)
    await orch.tick()
    assert await store.add_guidance(run.id, "web", "alice", "add tests")

    await orch.tick()
    waited = await latest_call(store, run)
    assert waited.status == "succeeded"
    assert waited.result == {
        "kind": "feedback",
        "items": [{"author": "a person overseeing this work", "body": "add tests"}],
    }

    await orch.tick()
    fixing = await latest_call(store, run)
    assert fixing.name == "fix"
    assert "add tests" in str(fixing.args["findings"])

    await report(store, runner, fixing)
    await orch.tick()
    second_wait = await latest_call(store, run)
    assert (second_wait.step_index, second_wait.kind) == (2, "wait")

    VERDICTS.append(Passed())
    await orch.tick()
    await orch.tick()
    assert (await get_run(store, run)).status == "succeeded"


async def test_wait_that_stops_the_run_cancels_it(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, until_green)
    VERDICTS.append(Stopped())

    await orch.tick()
    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    assert runner.deleted_workspaces == {run.id}
    assert (await timeline(store, run))[-1] == "run cancelled"


async def test_restarted_orchestrator_continues_from_the_journal(
    store: Store, runner: FakeRunner
) -> None:
    run = await make_run(store, two_steps, "42")
    await orchestrator(store, runner).tick()
    first = await latest_call(store, run)
    await report(store, runner, first, "fledge/42")

    await orchestrator(store, runner).tick()

    assert [s.step_run_id for s in runner.started] == [
        first.id,
        (await latest_call(store, run)).id,
    ]
    assert (await latest_call(store, run)).args == {"branch": "fledge/42"}


async def test_sandbox_reaches_the_step_spec(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    await make_run(store, sandboxed)

    await orch.tick()

    [spec] = runner.started
    assert spec.services == (DB, CACHE, SEARCH)
    assert spec.env["MODE"] == "test"
    assert spec.env[READY_PORTS_ENV] == "db:5432,search:9200"


async def test_step_env_carries_wire_settings_and_secrets(
    store: Store, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = "gh-value"
    monkeypatch.setenv("GH_TOKEN", configured)
    monkeypatch.setenv("UNRELATED", "nope")
    orch = orchestrator(store, runner, step_secrets=["GH_TOKEN"])
    run = await make_run(store, two_steps)

    await orch.tick()

    [spec] = runner.started
    step_run = await latest_call(store, run)
    assert spec.step_run_id == step_run.id
    assert spec.run_id == run.id
    assert spec.image == "fledge:test"
    assert spec.services == ()
    assert set(spec.env) == {API_URL_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV, "GH_TOKEN"}
    assert spec.env[API_URL_ENV] == "http://fledge:8000"
    assert spec.env[STEP_RUN_ID_ENV] == str(step_run.id)
    assert hash_token(spec.env[STEP_TOKEN_ENV]) == step_run.token_hash
    assert spec.env["GH_TOKEN"] == configured


async def test_unset_secret_is_not_passed_and_is_logged(
    store: Store,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = "gh-value"
    monkeypatch.setenv("GH_TOKEN", configured)
    monkeypatch.delenv("FLEDGE_TEST_ABSENT", raising=False)
    orch = orchestrator(store, runner, step_secrets=["GH_TOKEN", "FLEDGE_TEST_ABSENT"])
    await make_run(store, two_steps)

    await orch.tick()

    [spec] = runner.started
    assert spec.env["GH_TOKEN"] == configured
    assert "FLEDGE_TEST_ABSENT" not in spec.env
    assert "FLEDGE_TEST_ABSENT" in caplog.text


async def test_one_broken_run_does_not_block_others(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    broken = await make_run(store, two_steps, "1")
    healthy = await make_run(store, two_steps, "2")
    runner.failing_workspaces.add(broken.id)

    await orch.tick()

    assert (await get_run(store, broken)).status == "running"
    assert await journal(store, broken) == []
    assert (await latest_call(store, healthy)).status == "running"
