from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from fledge import engine
from fledge.agents import Agent, Review, run_turn
from fledge.checks import Feedback, FeedbackItem, until
from fledge.control.store import CallRow
from fledge.core import Repo, Run, Sandbox, WorkItem
from fledge.engine import (
    Failed,
    Outcome,
    PendingCall,
    Succeeded,
    Suspended,
    Workflow,
    workflow,
)
from fledge.github import GitHubIssues
from fledge.harnesses.base import Turn, TurnResult

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WORK = WorkItem(
    external_id="12",
    title="Add a greeting",
    body="Say hi.",
    url="https://github.com/acme/api/issues/12",
    repo_url="https://github.com/acme/api",
)
TRIGGER = GitHubIssues("acme/api", "fledge")
REPO = Repo("https://github.com/acme/api", Path("/w/repos/api"), "fledge/abc", "main")
REPO_JSON: JsonValue = {
    "url": "https://github.com/acme/api",
    "path": "/w/repos/api",
    "branch": "fledge/abc",
    "base": "main",
}


@dataclass(frozen=True)
class Decision:
    docs_only: bool
    reason: str


# A step rebuilds the harness from its fields, so what the turns were asked is
# recorded here rather than on the instance.
TURNS: list[Turn] = []


@dataclass(frozen=True)
class FakeHarness:
    """Answers each turn in order; records what it was asked in TURNS."""

    structured: JsonValue = None

    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        TURNS.append(turn)
        number = len(TURNS)
        on_event(f"turn {number}")
        return TurnResult(f"session-{number}", f"done {number}", self.structured)


def agent_json(name: str, structured: JsonValue = None) -> JsonValue:
    return {
        "name": name,
        "harness_type": "test_agents:FakeHarness",
        "harness": {"structured": structured},
    }


def local_harness() -> FakeHarness:
    @dataclass(frozen=True)
    class Local(FakeHarness):
        pass

    return Local()


sandbox = Sandbox(env={"A": "1"})
implementer = Agent("implementer", FakeHarness(), sandbox=sandbox)
reviewer = Agent("reviewer", FakeHarness())


def turn_output(message: str, structured: JsonValue = None) -> JsonValue:
    return {"message": message, "structured": structured}


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


def test_prompt_runs_a_turn_step_and_returns_the_final_message() -> None:
    answers: list[str] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        answers.append(implementer.prompt("do it", REPO))

    outcome, [call] = drive(flow, [turn_output("did it")])

    assert outcome == Succeeded()
    assert answers[-1] == "did it"
    assert (call.kind, call.name, call.function, call.sandbox) == (
        "step",
        "implementer",
        "fledge.agents:run_turn",
        sandbox,
    )
    assert call.args == {
        "agent": agent_json("implementer"),
        "prompt": "do it",
        "repo": REPO_JSON,
        "schema": None,
    }


def test_an_agent_refuses_a_harness_a_step_container_could_not_rebuild() -> None:
    class Opaque:
        def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
            raise NotImplementedError

    with pytest.raises(TypeError, match="module level"):
        Agent("local", local_harness())
    with pytest.raises(TypeError, match="dataclass or Pydantic model"):
        Agent("opaque", Opaque())


def test_prompt_with_feedback_renders_it() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        implementer.prompt(Feedback([FeedbackItem("ci", "tests fail")]), REPO)

    outcome = engine.replay(flow, Run(uuid4(), WORK), START, [])

    assert isinstance(outcome, Suspended)
    prompt = outcome.call.args["prompt"]
    assert isinstance(prompt, str)
    assert prompt.startswith("Address these findings")
    assert "From ci:\ntests fail" in prompt


def test_prompt_with_returns_gives_back_a_validated_value() -> None:
    decisions: list[Decision] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        decisions.append(implementer.prompt("decide", REPO, returns=Decision))

    outcome, [call] = drive(
        flow, [turn_output("{}", {"docs_only": True, "reason": "README only"})]
    )

    assert outcome == Succeeded()
    assert decisions[-1] == Decision(docs_only=True, reason="README only")
    assert call.args["schema"] == TypeAdapter(Decision).json_schema()


def test_prompt_with_returns_fails_the_run_on_a_value_of_the_wrong_shape() -> None:
    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        implementer.prompt("decide", REPO, returns=Decision)

    outcome, _ = drive(flow, [turn_output("{}", {"docs_only": "maybe"})])

    assert isinstance(outcome, Failed)
    assert outcome.error.startswith("ValidationError")


def test_approves_maps_comments_to_feedback_and_approval_to_passed() -> None:
    received: list[Feedback] = []

    @workflow(trigger=TRIGGER)
    def flow(run: Run) -> None:
        del run
        received.clear()
        for found in until(reviewer.approves("review it", REPO), max_rounds=2):
            received.append(found)
            implementer.prompt(found, REPO)

    outcome, calls = drive(
        flow,
        [
            turn_output("no", {"approved": False, "comments": ["rename", "test"]}),
            turn_output("fixed"),
            turn_output("yes", {"approved": True, "comments": []}),
        ],
    )

    assert outcome == Succeeded()
    assert received == [
        Feedback([FeedbackItem("reviewer", "rename"), FeedbackItem("reviewer", "test")])
    ]
    assert [c.name for c in calls] == ["reviewer", "implementer", "reviewer"]
    assert calls[0].args["schema"] == TypeAdapter(Review).json_schema()
    assert calls[0].args["prompt"] == "review it"
    assert calls[0].sandbox == Sandbox()


@dataclass
class FakeContext:
    workspace: Path
    pending_guidance: list[list[str]] = field(default_factory=list)
    work: WorkItem = WORK
    run_id: UUID = field(default_factory=uuid4)
    logs: list[str] = field(default_factory=list)
    agent_events: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def agent_event(self, message: str) -> None:
        self.agent_events.append(message)

    def claim_guidance(self) -> list[str]:
        return self.pending_guidance.pop(0) if self.pending_guidance else []


def turn_args(
    workspace: Path,
    prompt: str,
    schema: JsonValue = None,
    structured: JsonValue = None,
) -> dict[str, JsonValue]:
    return {
        "agent": agent_json("dev", structured),
        "prompt": prompt,
        "repo": {
            "url": "https://github.com/acme/api",
            "path": str(workspace / "repos" / "api"),
            "branch": "fledge/abc",
            "base": "main",
        },
        "schema": schema,
    }


@pytest.fixture
def ctx(tmp_path: Path) -> FakeContext:
    TURNS.clear()
    return FakeContext(tmp_path)


def test_run_turn_starts_a_session_and_resumes_it_next_time(ctx: FakeContext) -> None:
    first = run_turn.execute(ctx, turn_args(ctx.workspace, "start"))
    second = run_turn.execute(ctx, turn_args(ctx.workspace, "continue"))

    home = ctx.workspace / "agents" / "dev"
    assert first == turn_output("done 1")
    assert second == turn_output("done 2")
    assert [(t.prompt, t.session_id) for t in TURNS] == [
        ("start", None),
        ("continue", "session-1"),
    ]
    assert (home / "session").read_text() == "session-2"
    assert all(t.state_dir == home / "state" for t in TURNS)
    assert (home / "state").is_dir()
    assert all(t.workdir == ctx.workspace / "repos" / "api" for t in TURNS)
    assert all(t.output_schema is None for t in TURNS)
    assert ctx.agent_events == ["turn 1", "turn 2"]


def test_run_turn_delivers_guidance_as_more_turns_in_the_session(
    ctx: FakeContext,
) -> None:
    ctx.pending_guidance = [["use tabs", "add a test"], ["and docs"], [], ["later"]]

    result = run_turn.execute(ctx, turn_args(ctx.workspace, "start"))

    assert result == turn_output("done 3")
    assert [t.session_id for t in TURNS] == [None, "session-1", "session-2"]
    assert "Message 1:\nuse tabs\n\nMessage 2:\nadd a test" in TURNS[1].prompt
    assert "Message 1:\nand docs" in TURNS[2].prompt
    assert ctx.pending_guidance == [["later"]]


def test_run_turn_gives_the_harness_the_schema_and_returns_its_answer(
    ctx: FakeContext,
) -> None:
    schema = TypeAdapter(Decision).json_schema()
    answer: JsonValue = {"docs_only": False, "reason": "code"}

    result = run_turn.execute(ctx, turn_args(ctx.workspace, "decide", schema, answer))

    assert result == turn_output("done 1", answer)
    [turn] = TURNS
    assert turn.output_schema == schema


def test_run_turn_fails_without_a_structured_result(ctx: FakeContext) -> None:
    schema = TypeAdapter(Review).json_schema()
    with pytest.raises(RuntimeError, match="without a structured result"):
        run_turn.execute(ctx, turn_args(ctx.workspace, "review", schema))
