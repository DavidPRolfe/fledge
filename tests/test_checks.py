import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from fledge import engine
from fledge.checks import (
    Feedback,
    FeedbackItem,
    Passed,
    Pending,
    RoundsExhausted,
    Stopped,
    Verdict,
    WaitContext,
    check,
    command,
    guidance_verdict,
    render_feedback,
    until,
    wait,
)
from fledge.control.store import CallRow
from fledge.core import Repo, Run, Sandbox, StepContext, WorkItem
from fledge.engine import (
    Cancelled,
    Failed,
    Outcome,
    PendingCall,
    Succeeded,
    Suspended,
    Workflow,
    step,
    workflow,
)
from fledge.github import GitHubIssues

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WORK = WorkItem(
    external_id="12",
    title="Add a greeting",
    body="Say hi.",
    url="https://github.com/acme/api/issues/12",
    repo_url="https://github.com/acme/api",
)
TRIGGER = GitHubIssues("acme/api", "fledge")
PASSED: JsonValue = {"kind": "passed"}
STOPPED: JsonValue = {"kind": "stopped"}


def feedback(body: str) -> JsonValue:
    return {"kind": "feedback", "items": [{"author": "ci", "body": body}]}


def finished_at(position: int) -> datetime:
    return START + timedelta(minutes=position + 1)


def entry(run: Run, call: PendingCall, result: JsonValue) -> CallRow:
    return CallRow(
        id=uuid4(),
        run_id=run.id,
        position=call.position,
        kind=call.kind,
        name=call.name,
        function=call.function,
        args=call.args,
        status="succeeded",
        result=result,
        error=None,
        token_hash="",
        started_at=finished_at(call.position) - timedelta(seconds=30),
        finished_at=finished_at(call.position),
    )


def drive(
    flow: Workflow, results: Sequence[JsonValue]
) -> tuple[Outcome, list[PendingCall]]:
    """Replay `flow`, recording each result for the call it suspends on."""
    run = Run(uuid4(), WORK)
    journal: list[CallRow] = []
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

    def log(self, message: str) -> None:
        del message

    def agent_event(self, message: str) -> None:
        del message

    def claim_guidance(self) -> list[str]:
        return []


@check
def file_exists(ctx: StepContext, name: str) -> Passed | Feedback:
    if (ctx.workspace / name).exists():
        return Passed()
    return Feedback([FeedbackItem("file_exists", f"{name} is missing")])


@step
def fix(ctx: StepContext, attempt: int) -> int:
    del ctx
    return attempt


@wait
def file_verdict(ctx: WaitContext, path: Path) -> Verdict:
    """Reads its verdict from a file; echoes `since` in feedback."""
    text = path.read_text()
    if text == "pending":
        return Pending()
    if text == "passed":
        return Passed()
    if text == "stopped":
        return Stopped()
    return Feedback([FeedbackItem(ctx.run.work.external_id, ctx.since.isoformat())])


def fixing_flow(max_rounds: int, received: list[Feedback]) -> Workflow:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        # Each replay runs the body again from the top.
        received.clear()
        for attempt, found in enumerate(
            until(file_exists("out.txt"), max_rounds=max_rounds)
        ):
            received.append(found)
            fix(attempt)

    return flow


def test_until_ends_at_once_when_the_check_passes() -> None:
    received: list[Feedback] = []

    outcome, calls = drive(fixing_flow(3, received), [PASSED])

    assert outcome == Succeeded()
    assert received == []
    assert [c.function for c in calls] == ["test_checks:file_exists"]


def test_until_yields_feedback_until_the_check_passes() -> None:
    received: list[Feedback] = []

    outcome, calls = drive(
        fixing_flow(3, received), [feedback("first"), 0, feedback("second"), 1, PASSED]
    )

    assert outcome == Succeeded()
    assert received == [
        Feedback([FeedbackItem("ci", "first")]),
        Feedback([FeedbackItem("ci", "second")]),
    ]
    assert [c.name for c in calls] == [
        "file_exists",
        "fix",
        "file_exists",
        "fix",
        "file_exists",
    ]


@pytest.mark.parametrize("max_rounds", [0, 1, 2])
def test_until_raises_rounds_exhausted_after_max_rounds(max_rounds: int) -> None:
    received: list[Feedback] = []
    rounds = [result for n in range(max_rounds) for result in (feedback(str(n)), n)]

    # One evaluation per round, and one more that still has feedback.
    outcome, calls = drive(fixing_flow(max_rounds, received), [*rounds, feedback("x")])

    assert outcome == Failed(
        f"RoundsExhausted: still not passing after {max_rounds} rounds"
    )
    assert len(calls) == 2 * max_rounds + 1
    assert len(received) == max_rounds


def test_rounds_exhausted_can_be_caught() -> None:
    caught: list[str] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        try:
            for _ in until(file_exists("out.txt"), max_rounds=0):
                pass
        except RoundsExhausted as err:
            caught.append(str(err))

    outcome, _ = drive(flow, [feedback("x")])

    assert outcome == Succeeded()
    assert caught == ["still not passing after 0 rounds"]


def test_until_cancels_the_run_when_a_wait_stops(tmp_path: Path) -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        try:
            for _ in until(file_verdict(tmp_path / "v"), max_rounds=3):
                pass
        except Exception:  # noqa: BLE001 - checks that RunStopped is not an Exception
            fix(0)

    outcome, calls = drive(flow, [STOPPED])

    assert outcome == Cancelled()
    assert len(calls) == 1


def test_a_check_is_a_container_call_with_its_arguments(tmp_path: Path) -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        for _ in until(file_exists("out.txt"), max_rounds=1):
            pass

    outcome = engine.replay(flow, Run(uuid4(), WORK), START, [])

    assert isinstance(outcome, Suspended)
    call = outcome.call
    assert (call.kind, call.name, call.function, call.args) == (
        "step",
        "file_exists",
        "test_checks:file_exists",
        {"name": "out.txt"},
    )
    assert call.poll is None
    resolved = engine.resolve_step(call.function)
    ctx = FakeContext(tmp_path)
    assert resolved.execute(ctx, call.args) == feedback_from("out.txt is missing")
    (tmp_path / "out.txt").touch()
    assert resolved.execute(ctx, call.args) == PASSED


def feedback_from(body: str) -> JsonValue:
    return {"kind": "feedback", "items": [{"author": "file_exists", "body": body}]}


def test_within_runs_the_check_with_another_sandbox() -> None:
    sandbox = Sandbox(env={"CI": "1"})

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        for _ in until(file_exists.within(sandbox)("out.txt"), max_rounds=1):
            pass

    outcome = engine.replay(flow, Run(uuid4(), WORK), START, [])

    assert isinstance(outcome, Suspended)
    assert outcome.call.sandbox == sandbox
    assert outcome.call.function == "test_checks:file_exists"


def suspended_wait(flow: Workflow, journal: list[CallRow], run: Run) -> PendingCall:
    outcome = engine.replay(flow, run, START, journal)
    assert isinstance(outcome, Suspended)
    assert outcome.call.kind == "wait"
    return outcome.call


def test_a_wait_polls_none_while_pending_and_json_once_settled(
    tmp_path: Path,
) -> None:
    verdict = tmp_path / "verdict"

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        for _ in until(file_verdict(verdict), max_rounds=1):
            pass

    call = suspended_wait(flow, [], Run(uuid4(), WORK))
    assert call.poll is not None
    assert (call.name, call.function, call.sandbox) == (
        "file_verdict",
        "test_checks:file_verdict",
        Sandbox(),
    )
    assert call.args == {"path": str(verdict), "since": START.isoformat()}

    verdict.write_text("pending")
    assert call.poll(START) is None
    verdict.write_text("passed")
    assert call.poll(START) == PASSED
    verdict.write_text("stopped")
    assert call.poll(START) == STOPPED
    verdict.write_text("feedback")
    assert call.poll(START) == {
        "kind": "feedback",
        "items": [{"author": "12", "body": START.isoformat()}],
    }


def test_since_is_the_loop_start_then_when_feedback_was_recorded(
    tmp_path: Path,
) -> None:
    verdict = tmp_path / "verdict"
    verdict.write_text("feedback")

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        fix(0)
        for attempt, _ in enumerate(until(file_verdict(verdict), max_rounds=2)):
            fix(attempt + 1)

    run = Run(uuid4(), WORK)
    journal: list[CallRow] = []
    outcome = engine.replay(flow, run, START, journal)
    assert isinstance(outcome, Suspended)
    journal.append(entry(run, outcome.call, 0))

    first = suspended_wait(flow, journal, run)
    assert first.args["since"] == finished_at(0).isoformat()
    assert first.poll is not None
    first_result = first.poll(START)
    assert first_result == {
        "kind": "feedback",
        "items": [{"author": "12", "body": finished_at(0).isoformat()}],
    }
    journal.append(entry(run, first, first_result))

    outcome = engine.replay(flow, run, START, journal)
    assert isinstance(outcome, Suspended)
    assert outcome.call.name == "fix"
    journal.append(entry(run, outcome.call, 1))

    second = suspended_wait(flow, journal, run)
    assert second.args["since"] == finished_at(1).isoformat()
    assert second.poll is not None
    assert second.poll(START) == {
        "kind": "feedback",
        "items": [{"author": "12", "body": finished_at(1).isoformat()}],
    }


def test_guidance_recorded_for_a_wait_arrives_as_feedback(tmp_path: Path) -> None:
    received: list[Feedback] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        received.clear()
        received.extend(until(file_verdict(tmp_path / "v"), max_rounds=1))

    outcome, _ = drive(flow, [guidance_verdict(["use tabs", "add a test"]), PASSED])

    assert outcome == Succeeded()
    assert received == [
        Feedback(
            [
                FeedbackItem("a person overseeing this work", "use tabs"),
                FeedbackItem("a person overseeing this work", "add a test"),
            ]
        )
    ]


def test_render_feedback_names_each_author() -> None:
    text = render_feedback(
        Feedback([FeedbackItem("ci", "tests fail"), FeedbackItem("bob", "rename")])
    )

    assert text.startswith("Address these findings")
    assert "From ci:\ntests fail\n\nFrom bob:\nrename" in text


def repo_at(path: Path) -> Repo:
    return Repo("https://github.com/acme/api", path, "fledge/x", "main")


def repo_json(path: Path) -> dict[str, JsonValue]:
    return {
        "url": "https://github.com/acme/api",
        "path": str(path),
        "branch": "fledge/x",
        "base": "main",
    }


def python(code: str) -> list[JsonValue]:
    # Unbuffered, so stdout and stderr interleave in the order they are written.
    return [sys.executable, "-u", "-c", code]


def test_command_passes_in_the_repository(tmp_path: Path) -> None:
    (tmp_path / "marker").write_text("")
    argv = python(
        "import pathlib, sys; sys.exit(0 if pathlib.Path('marker').exists() else 3)"
    )

    result = command.step.execute(
        FakeContext(tmp_path), {"argv": argv, "repo": repo_json(tmp_path)}
    )

    assert result == {"kind": "passed"}


def test_command_failure_is_feedback_with_the_tail_of_the_output(
    tmp_path: Path,
) -> None:
    argv = python(
        "import sys\n"
        "for n in range(150): print(f'out {n}')\n"
        "print('err line', file=sys.stderr)\n"
        "sys.exit(4)"
    )

    result = command.step.execute(
        FakeContext(tmp_path), {"argv": argv, "repo": repo_json(tmp_path)}
    )

    feedback = TypeAdapter(Passed | Feedback).validate_python(result)
    assert isinstance(feedback, Feedback)
    [item] = feedback.items
    assert item.author == sys.executable
    assert "exited with status 4" in item.body
    tail = item.body.split("End of its output:\n")[1].splitlines()
    assert tail == [f"out {n}" for n in range(51, 150)] + ["err line"]


def test_command_within_a_sandbox_records_the_sandbox(tmp_path: Path) -> None:
    sandbox = Sandbox(env={"A": "1"})

    @workflow(trigger=TRIGGER)
    def checked(run: Run) -> None:
        for _ in until(
            command.within(sandbox)(["make"], repo_at(tmp_path)), max_rounds=1
        ):
            pass

    outcome = engine.replay(checked, Run(uuid4(), WORK), START, [])

    assert isinstance(outcome, engine.Suspended)
    assert outcome.call.sandbox == sandbox
    assert outcome.call.function == "fledge.checks:command"
    assert outcome.call.args["argv"] == ["make"]
