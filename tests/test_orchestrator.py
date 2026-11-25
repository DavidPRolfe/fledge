from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from fledge.core import StepContext, Workflow, WorkItem
from fledge.orchestrator import Orchestrator
from fledge.runner.base import ContainerState, StepSpec
from fledge.settings import Settings
from fledge.store import Run, StepRun, Store, hash_token
from fledge.wire import API_URL_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class FakeStep:
    name: str

    def run(self, ctx: StepContext) -> dict[str, str]:
        raise AssertionError("steps run in containers, not in the orchestrator")


WORKFLOWS = {
    "two_steps": Workflow(name="two_steps", steps=[FakeStep("a"), FakeStep("b")]),
    "one_step": Workflow(name="one_step", steps=[FakeStep("only")]),
}


@dataclass
class FakeRunner:
    workspaces: set[UUID] = field(default_factory=set)
    deleted_workspaces: set[UUID] = field(default_factory=set)
    started: list[StepSpec] = field(default_factory=list)
    stopped: set[UUID] = field(default_factory=set)
    states: dict[UUID, ContainerState] = field(default_factory=dict)
    before_state: Callable[[UUID], Awaitable[None]] | None = None

    async def create_workspace(self, run_id: UUID) -> None:
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
            "max_concurrent_runs": 5,
            **overrides,
        }
    )


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with Store(database_url) as store:
        yield store


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def orchestrator(store: Store, runner: FakeRunner, **config: object) -> Orchestrator:
    return Orchestrator(store, runner, make_config(**config), WORKFLOWS.__getitem__)


async def make_run(store: Store, workflow: str, external_id: str = "1") -> Run:
    work = WorkItem(
        external_id=external_id,
        title="Fix it",
        body="",
        url=f"https://example.test/{external_id}",
        repo_url="https://example.test/repo.git",
    )
    return await store.create_manual_run("issues", workflow, work)


async def get_run(store: Store, run: Run) -> Run:
    current = await store.get_run(run.id)
    assert current is not None
    return current


async def latest_step(store: Store, run: Run) -> StepRun:
    return (await store.list_step_runs(run.id))[-1]


async def report(
    store: Store, runner: FakeRunner, step_run: StepRun, *, ok: bool = True
) -> None:
    """Post a result as the internal API would, then let the container exit."""
    if ok:
        assert await store.finish_step_run(step_run.id, "succeeded", {"k": "v"})
    else:
        assert await store.finish_step_run(step_run.id, "failed", {}, "boom")
    runner.states[step_run.id] = ContainerState.EXITED


async def timeline(store: Store, run: Run) -> list[str]:
    return [event.message for event in await store.list_events(run.id)]


async def test_run_goes_through_every_step_to_success(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "two_steps")

    await orch.tick()
    assert (await get_run(store, run)).status == "running"
    assert runner.workspaces == {run.id}
    first = await latest_step(store, run)
    assert (first.step_index, first.name, first.status) == (0, "a", "running")

    await orch.tick()
    assert len(runner.started) == 1

    await report(store, runner, first)
    await orch.tick()
    second = await latest_step(store, run)
    assert (second.step_index, second.name) == (1, "b")
    assert first.id in runner.stopped
    assert (await get_run(store, run)).status == "running"

    await report(store, runner, second)
    await orch.tick()
    finished = await get_run(store, run)
    assert finished.status == "succeeded"
    assert finished.error is None
    assert second.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert [s.step_run_id for s in runner.started] == [first.id, second.id]
    assert await timeline(store, run) == [
        "run started",
        "step a started",
        "step a succeeded",
        "step b started",
        "step b succeeded",
        "run succeeded",
    ]

    await orch.tick()
    assert len(runner.started) == 2


async def test_concurrency_limit_promotes_oldest_first(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, max_concurrent_runs=2)
    runs = [await make_run(store, "one_step", str(n)) for n in range(3)]

    await orch.tick()
    statuses = [(await get_run(store, run)).status for run in runs]
    assert statuses == ["running", "running", "pending"]

    await orch.tick()
    assert (await get_run(store, runs[2])).status == "pending"

    await report(store, runner, await latest_step(store, runs[0]))
    await orch.tick()
    assert (await get_run(store, runs[0])).status == "succeeded"
    await orch.tick()
    assert (await get_run(store, runs[2])).status == "running"


async def test_failed_step_fails_the_run_and_cleans_up(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "two_steps")
    await orch.tick()
    step = await latest_step(store, run)

    await report(store, runner, step, ok=False)
    await orch.tick()

    failed = await get_run(store, run)
    assert (failed.status, failed.error) == ("failed", "boom")
    assert step.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert len(await store.list_step_runs(run.id)) == 1
    assert (await timeline(store, run))[-2:] == [
        "step a failed: boom",
        "run failed: boom",
    ]


@pytest.mark.parametrize("state", [ContainerState.EXITED, ContainerState.MISSING])
async def test_container_gone_without_result_fails_the_run(
    store: Store, runner: FakeRunner, state: ContainerState
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "two_steps")
    await orch.tick()
    step = await latest_step(store, run)

    runner.states[step.id] = state
    await orch.tick()

    error = "step container exited without reporting a result"
    current = await store.get_step_run(step.id)
    assert current is not None
    assert (current.status, current.error) == ("failed", error)
    failed = await get_run(store, run)
    assert (failed.status, failed.error) == ("failed", error)
    assert runner.deleted_workspaces == {run.id}


async def test_result_posted_just_before_exit_is_honoured(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "one_step")
    await orch.tick()
    step = await latest_step(store, run)

    async def post_then_exit(step_run_id: UUID) -> None:
        # The result lands after the tick listed the step runs as running.
        assert await store.finish_step_run(step_run_id, "succeeded", {"k": "v"})
        runner.states[step_run_id] = ContainerState.EXITED

    runner.before_state = post_then_exit
    await orch.tick()

    current = await store.get_step_run(step.id)
    assert current is not None
    assert (current.status, current.output) == ("succeeded", {"k": "v"})

    await orch.tick()
    assert (await get_run(store, run)).status == "succeeded"


async def test_step_past_its_timeout_is_stopped_and_fails(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, step_timeout_minutes=0)
    run = await make_run(store, "two_steps")
    await orch.tick()
    step = await latest_step(store, run)
    assert step.status == "running"

    await orch.tick()

    error = "step timed out after 0 minutes"
    current = await store.get_step_run(step.id)
    assert current is not None
    assert (current.status, current.error) == ("failed", error)
    assert step.id in runner.stopped
    failed = await get_run(store, run)
    assert (failed.status, failed.error) == ("failed", error)


async def test_step_within_its_timeout_keeps_running(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner, step_timeout_minutes=60)
    run = await make_run(store, "two_steps")
    await orch.tick()
    await orch.tick()
    assert (await latest_step(store, run)).status == "running"
    assert runner.stopped == set()


async def test_cancel_while_pending(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "two_steps")
    assert await store.request_cancel(run.id)

    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    assert runner.workspaces == set()
    assert runner.started == []
    assert await timeline(store, run) == ["run cancelled"]


async def test_cancel_while_running(store: Store, runner: FakeRunner) -> None:
    orch = orchestrator(store, runner)
    run = await make_run(store, "two_steps")
    await orch.tick()
    step = await latest_step(store, run)
    assert await store.request_cancel(run.id)

    await orch.tick()

    assert (await get_run(store, run)).status == "cancelled"
    current = await store.get_step_run(step.id)
    assert current is not None
    assert current.status == "cancelled"
    assert step.id in runner.stopped
    assert runner.deleted_workspaces == {run.id}
    assert (await timeline(store, run))[-2:] == ["step a cancelled", "run cancelled"]


async def test_step_env_carries_wire_settings_and_secrets(
    store: Store, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = "gh-value"
    monkeypatch.setenv("GH_TOKEN", configured)
    monkeypatch.setenv("UNRELATED", "nope")
    orch = orchestrator(store, runner, step_secrets=["GH_TOKEN"])
    run = await make_run(store, "two_steps")

    await orch.tick()

    [spec] = runner.started
    step = await latest_step(store, run)
    assert spec.step_run_id == step.id
    assert spec.run_id == run.id
    assert spec.image == "fledge:test"
    assert set(spec.env) == {API_URL_ENV, STEP_RUN_ID_ENV, STEP_TOKEN_ENV, "GH_TOKEN"}
    assert spec.env[API_URL_ENV] == "http://fledge:8000"
    assert spec.env[STEP_RUN_ID_ENV] == str(step.id)
    assert hash_token(spec.env[STEP_TOKEN_ENV]) == step.token_hash
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
    await make_run(store, "two_steps")

    await orch.tick()

    [spec] = runner.started
    assert spec.env["GH_TOKEN"] == configured
    assert "FLEDGE_TEST_ABSENT" not in spec.env
    assert "FLEDGE_TEST_ABSENT" in caplog.text


async def test_one_broken_run_does_not_block_others(
    store: Store, runner: FakeRunner
) -> None:
    orch = orchestrator(store, runner)
    broken = await make_run(store, "no_such_workflow", "1")
    healthy = await make_run(store, "one_step", "2")

    await orch.tick()

    assert (await get_run(store, broken)).status == "running"
    assert await store.list_step_runs(broken.id) == []
    assert (await latest_step(store, healthy)).status == "running"
