from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from fledge import engine
from fledge.control.store import JournalRow, StepStatus
from fledge.core import Run, Sandbox, Service, StepContext, WorkItem
from fledge.engine import (
    Cancelled,
    Failed,
    Outcome,
    PendingCall,
    RunStopped,
    StepFailed,
    Succeeded,
    Suspended,
    Workflow,
    step,
    workflow,
)
from fledge.github import GitHubIssues
from fledge.harnesses.base import Harness

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WORK = WorkItem(
    external_id="12",
    title="Add a greeting",
    body="Say hi.",
    url="https://github.com/acme/api/issues/12",
    repo_url="https://github.com/acme/api",
)
TRIGGER = GitHubIssues("acme/api", "fledge")


@dataclass(frozen=True)
class Fails:
    """A journal result for a call that failed."""

    error: str


type Result = JsonValue | Fails


def finished_at(position: int) -> datetime:
    return START + timedelta(minutes=position + 1)


def entry(run: Run, call: PendingCall, result: Result) -> JournalRow:
    if isinstance(result, Fails):
        status: StepStatus = "failed"
        value: JsonValue = None
        error: str | None = result.error
    else:
        status, value, error = "succeeded", result, None
    return JournalRow(
        id=uuid4(),
        run_id=run.id,
        step_index=call.position,
        kind=call.kind,
        name=call.name,
        function=call.function,
        args=call.args,
        status=status,
        result=value,
        error=error,
        token_hash="",
        started_at=finished_at(call.position) - timedelta(seconds=30),
        finished_at=finished_at(call.position),
    )


def drive(
    flow: Workflow, results: Sequence[Result]
) -> tuple[Outcome, list[PendingCall]]:
    """Replay `flow`, recording each result for the call it suspends on."""
    run = Run(uuid4(), WORK)
    journal: list[JournalRow] = []
    calls: list[PendingCall] = []
    for result in results:
        outcome = engine.replay(flow, run, START, journal)
        assert isinstance(outcome, Suspended), outcome
        calls.append(outcome.call)
        journal.append(entry(run, outcome.call, result))
    return engine.replay(flow, run, START, journal), calls


@dataclass
class FakeContext:
    workspace: Path
    work: WorkItem = WORK
    run_id: UUID = field(default_factory=uuid4)
    logs: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def agent_event(self, message: str) -> None:
        self.logs.append(message)

    def claim_guidance(self) -> list[str]:
        return []

    def harness(self, agent: str) -> Harness:
        raise KeyError(agent)


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@step
def make_point(ctx: StepContext, x: int) -> Point:
    ctx.log(f"making {x}")
    return Point(x, 2 * x)


@step
def where(ctx: StepContext, point: Point) -> Path:
    return ctx.workspace / f"{point.x}-{point.y}"


@step
def nothing(ctx: StepContext) -> None:
    ctx.log("nothing")


@step
def numbers(ctx: StepContext, count: int) -> list[int]:
    del ctx
    return list(range(count))


@step
def double(ctx: StepContext, n: int) -> int:
    del ctx
    return 2 * n


SERVICES = Sandbox(services=[Service("postgres", "postgres:17", ready_port=5432)])


@step(sandbox=SERVICES)
def with_services(ctx: StepContext) -> str:
    del ctx
    return "ok"


def test_results_return_by_position_with_their_types() -> None:
    seen: list[object] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        point = make_point(1)
        seen.append(point)
        seen.append(where(point))
        seen.append(nothing())
        seen.append(numbers(2))

    outcome, calls = drive(flow, [{"x": 1, "y": 2}, "/w/1-2", None, [0, 1]])

    assert outcome == Succeeded()
    assert seen[-4:] == [Point(1, 2), Path("/w/1-2"), None, [0, 1]]
    assert [(c.position, c.kind, c.name, c.function, c.args) for c in calls] == [
        (0, "step", "make_point", "test_engine:make_point", {"x": 1}),
        (1, "step", "where", "test_engine:where", {"point": {"x": 1, "y": 2}}),
        (2, "step", "nothing", "test_engine:nothing", {}),
        (3, "step", "numbers", "test_engine:numbers", {"count": 2}),
    ]
    assert all(c.entry is None and c.poll is None for c in calls)


def test_the_same_step_in_a_loop_is_a_call_per_iteration() -> None:
    totals: list[int] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        totals.append(sum(double(n) for n in range(3)))

    outcome, calls = drive(flow, [0, 2, 4])

    assert outcome == Succeeded()
    assert totals[-1] == 6
    assert [c.args for c in calls] == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert [c.position for c in calls] == [0, 1, 2]


def test_a_failed_step_raises_step_failed_which_the_workflow_can_catch() -> None:
    caught: list[str] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        try:
            double(1)
        except StepFailed as err:
            caught.append(str(err))
        double(2)

    outcome, calls = drive(flow, [Fails("container died"), 4])

    assert outcome == Succeeded()
    assert caught[-1] == "container died"
    assert len(calls) == 2


def test_an_uncaught_step_failure_fails_the_run() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        double(1)
        double(2)

    outcome, _ = drive(flow, [Fails("timed out")])

    assert outcome == Failed("StepFailed: timed out")


def test_an_uncaught_exception_fails_the_run() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        if double(1) == 2:
            raise ValueError("two is not allowed")

    outcome, _ = drive(flow, [2])

    assert outcome == Failed("ValueError: two is not allowed")


def test_a_different_function_at_a_recorded_position_fails_the_run() -> None:
    calls_double = [True]

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        if calls_double[0]:
            double(1)
        else:
            numbers(1)

    run = Run(uuid4(), WORK)
    outcome = engine.replay(flow, run, START, [])
    assert isinstance(outcome, Suspended)
    journal = [entry(run, outcome.call, 2)]
    calls_double[0] = False

    outcome = engine.replay(flow, run, START, journal)

    assert isinstance(outcome, Failed)
    assert outcome.error.startswith("NondeterminismError: call 0 was recorded as ")
    assert "test_engine:double" in outcome.error
    assert "test_engine:numbers" in outcome.error


def stop() -> None:
    raise RunStopped


def test_run_stopped_is_not_swallowed_by_except_exception() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        try:
            double(1)
            stop()
        except Exception:  # noqa: BLE001 - the blind except is what is under test
            double(2)

    outcome, calls = drive(flow, [2])

    assert outcome == Cancelled()
    assert len(calls) == 1


def test_a_suspension_is_not_swallowed_by_except_exception() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        try:
            double(1)
        except Exception:  # noqa: BLE001 - the blind except is what is under test
            numbers(1)

    outcome = engine.replay(flow, Run(uuid4(), WORK), START, [])

    assert isinstance(outcome, Suspended)
    assert outcome.call.name == "double"


def test_the_clock_is_when_the_latest_passed_call_finished() -> None:
    clocks: list[datetime] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        clocks.append(engine.clock())
        double(1)
        clocks.append(engine.clock())
        double(2)
        clocks.append(engine.clock())

    outcome, _ = drive(flow, [2, 4])

    assert outcome == Succeeded()
    assert clocks[-3:] == [START, finished_at(0), finished_at(1)]


def test_the_journal_entry_of_a_started_call_is_on_the_pending_call() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        double(1)

    run = Run(uuid4(), WORK)
    first = engine.replay(flow, run, START, [])
    assert isinstance(first, Suspended)
    running = JournalRow(
        id=uuid4(),
        run_id=run.id,
        step_index=0,
        kind="step",
        name="double",
        function="test_engine:double",
        args={"n": 1},
        status="running",
        result=None,
        error=None,
        token_hash="",
        started_at=START,
        finished_at=None,
    )

    outcome = engine.replay(flow, run, START, [running])

    assert isinstance(outcome, Suspended)
    assert outcome.call.entry == running


def test_calling_a_step_outside_a_workflow_raises() -> None:
    with pytest.raises(RuntimeError, match="only be called in a workflow"):
        double(1)


def test_a_step_defined_inside_a_function_is_rejected() -> None:
    def local(ctx: StepContext) -> int:
        del ctx
        return 1

    with pytest.raises(TypeError, match="module level"):
        step(local)


def unannotated(ctx: StepContext, n) -> int:  # type: ignore[no-untyped-def]  # the missing annotation is what the test checks
    del ctx
    return int(n)


def test_an_unannotated_parameter_is_rejected() -> None:
    with pytest.raises(TypeError, match="parameter 'n' needs a type annotation"):
        step(unannotated)


def test_execute_rebuilds_arguments_and_dumps_the_result(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)

    assert make_point.execute(ctx, {"x": 3}) == {"x": 3, "y": 6}
    assert where.execute(ctx, {"point": {"x": 3, "y": 6}}) == str(tmp_path / "3-6")
    assert nothing.execute(ctx, {}) is None
    assert ctx.logs == ["making 3", "nothing"]


def test_resolve_step_finds_a_step_by_its_reference() -> None:
    assert engine.resolve_step("test_engine:double") is double
    with pytest.raises(TypeError, match="not a step"):
        engine.resolve_step("test_engine:finished_at")


def test_a_step_sandbox_reaches_the_pending_call() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        with_services()
        double(1)

    _, calls = drive(flow, ["ok", 2])

    assert calls[0].sandbox == SERVICES
    assert calls[1].sandbox == Sandbox()


def test_the_workflow_is_named_after_its_function() -> None:
    def ticket_to_pr(run: Run) -> None:
        del run

    made: Callable[[Callable[[Run], None]], Workflow] = workflow(trigger=TRIGGER)

    assert made(ticket_to_pr).name == "ticket_to_pr"
