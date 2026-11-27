"""Agents: a harness with a sandbox and a session that lasts the run."""

from dataclasses import dataclass
from datetime import datetime
from typing import overload

from pydantic import JsonValue, PydanticSchemaGenerationError, TypeAdapter

from fledge import engine
from fledge.checks import (
    Check,
    Feedback,
    FeedbackItem,
    Passed,
    Settled,
    render_feedback,
)
from fledge.core import NO_SANDBOX, Repo, Sandbox, StepContext
from fledge.engine import step
from fledge.git import prepare_agent_git
from fledge.harnesses.base import Harness, Turn


@dataclass(frozen=True)
class Review:
    """What `Agent.approves` asks the agent to finish with."""

    approved: bool
    comments: list[str]


@dataclass(frozen=True)
class TurnOutput:
    message: str
    structured: JsonValue


@dataclass(frozen=True)
class AgentRef:
    """An agent as a step container receives it: enough to rebuild its harness."""

    name: str
    harness_type: str
    """`module:qualname` of the harness class."""
    harness: JsonValue
    """The harness instance, dumped by pydantic."""


@dataclass(frozen=True)
class Agent:
    """A named harness. The name keys the agent's session, so two agents that
    one run prompts need different names.

    The harness must be an instance of a module-level dataclass or Pydantic
    model: the step container rebuilds it from its fields.
    """

    name: str
    harness: Harness
    sandbox: Sandbox = NO_SANDBOX

    def __post_init__(self) -> None:
        self._ref()

    def _ref(self) -> AgentRef:
        harness_type = type(self.harness)
        try:
            adapter: TypeAdapter[object] = TypeAdapter(harness_type)
        except PydanticSchemaGenerationError as err:
            raise TypeError(
                f"the harness of agent {self.name!r} must be a dataclass or "
                f"Pydantic model so that a step container can rebuild it, "
                f"got {harness_type.__qualname__}"
            ) from err
        return AgentRef(
            self.name,
            engine.ref(harness_type),
            adapter.dump_python(self.harness, mode="json"),
        )

    @overload
    def prompt(self, prompt: str | Feedback, repo: Repo) -> str: ...

    @overload
    def prompt[T](
        self, prompt: str | Feedback, repo: Repo, *, returns: type[T]
    ) -> T: ...

    def prompt[T](
        self, prompt: str | Feedback, repo: Repo, *, returns: type[T] | None = None
    ) -> str | T:
        """Run the agent in `repo` until it yields.

        Returns its final message, or with `returns` (a dataclass or Pydantic
        model) the value of that type it finished with.
        """
        text = render_feedback(prompt) if isinstance(prompt, Feedback) else prompt
        output = run_turn.call_as(
            self.name,
            self.sandbox,
            self._ref(),
            text,
            repo,
            None if returns is None else TypeAdapter(returns).json_schema(),
        )
        if returns is None:
            return output.message
        return TypeAdapter(returns).validate_python(output.structured)

    def approves(self, prompt: str, repo: Repo) -> Check:
        """A check that passes when the agent approves, with its comments as feedback."""
        return _Approval(self, prompt, repo)


@dataclass(frozen=True)
class _Approval:
    agent: Agent
    prompt: str
    repo: Repo

    def evaluate(self, since: datetime) -> Settled:
        del since
        review = self.agent.prompt(self.prompt, self.repo, returns=Review)
        if review.approved:
            return Passed()
        return Feedback([FeedbackItem(self.agent.name, c) for c in review.comments])


def _guidance_prompt(bodies: list[str]) -> str:
    messages = "\n\n".join(
        f"Message {number}:\n{body}" for number, body in enumerate(bodies, start=1)
    )
    return (
        "A person overseeing this work sent the following guidance. "
        "Take it into account and continue.\n\n" + messages
    )


@step
def run_turn(
    ctx: StepContext,
    agent: AgentRef,
    prompt: str,
    repo: Repo,
    schema: dict[str, JsonValue] | None,
) -> TurnOutput:
    """Prompt the agent, then keep going while guidance is waiting.

    `schema` is the JSON Schema its final answer must satisfy, if any.
    """
    adapter: TypeAdapter[object] = TypeAdapter(engine.resolve_type(agent.harness_type))
    harness = adapter.validate_python(agent.harness)
    if not isinstance(harness, Harness):
        raise TypeError(f"{agent.harness_type} is not a harness")
    # Outside every repository, so an agent's session never ends up in a commit.
    home = ctx.workspace / "agents" / agent.name
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    session_file = home / "session"
    session_id = session_file.read_text() if session_file.exists() else None
    prepare_agent_git()
    while True:
        turn = harness.run_turn(
            Turn(prompt, repo.path, state_dir, session_id, schema), ctx.agent_event
        )
        session_id = turn.session_id
        session_file.write_text(session_id)
        guidance = ctx.claim_guidance()
        if not guidance:
            break
        ctx.log(f"delivering {len(guidance)} guidance message(s) to the agent")
        prompt = _guidance_prompt(guidance)
    if schema is not None and turn.structured is None:
        raise RuntimeError(f"agent {agent.name} finished without a structured result")
    return TurnOutput(turn.final_message, turn.structured)
