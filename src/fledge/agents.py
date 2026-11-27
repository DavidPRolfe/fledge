"""Agents: a harness with a sandbox and a session that lasts the run."""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import overload

from pydantic import JsonValue, TypeAdapter

from fledge import engine
from fledge.checks import (
    Check,
    Feedback,
    FeedbackItem,
    Passed,
    Settled,
    render_feedback,
)
from fledge.core import NO_SANDBOX, Harness, Repo, Sandbox, StepContext, Turn
from fledge.engine import step

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - name of an env var, not a secret
_COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "fledge",
    "GIT_AUTHOR_EMAIL": "fledge@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "fledge",
    "GIT_COMMITTER_EMAIL": "fledge@users.noreply.github.com",
}


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
class Agent:
    """Define agents at module level, with names unique in the workflows directory."""

    name: str
    harness: Harness
    sandbox: Sandbox = NO_SANDBOX

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

        Returns its final message, or with `returns` (a module-level dataclass
        or Pydantic model) the value of that type it finished with.
        """
        text = render_feedback(prompt) if isinstance(prompt, Feedback) else prompt
        output = run_turn.call_as(
            self.name,
            self.sandbox,
            self.name,
            text,
            repo,
            None if returns is None else engine.ref(returns),
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


def _prepare_git() -> None:
    """Let the agent commit, and push with the configured token.

    The credential helper goes in the container's own git config, so the token
    is never written to the workspace.
    """
    for name, value in _COMMIT_IDENTITY.items():
        os.environ.setdefault(name, value)
    if os.environ.get(GITHUB_TOKEN_ENV):
        subprocess.run(
            ["gh", "auth", "setup-git"],  # noqa: S607 - gh is resolved from PATH in the step image
            check=True,
        )


@step
def run_turn(
    ctx: StepContext, agent: str, prompt: str, repo: Repo, returns: str | None
) -> TurnOutput:
    """Prompt the named agent, then keep going while guidance is waiting."""
    harness = ctx.agent(agent).harness
    schema: dict[str, JsonValue] | None = None
    if returns is not None:
        schema = TypeAdapter(engine.resolve_type(returns)).json_schema()
    # Outside every repository, so an agent's session never ends up in a commit.
    home = ctx.workspace / "agents" / agent
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    session_file = home / "session"
    session_id = session_file.read_text() if session_file.exists() else None
    _prepare_git()
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
        raise RuntimeError(f"agent {agent} finished without a structured result")
    return TurnOutput(turn.final_message, turn.structured)
