"""The types and interfaces that workflow code and extensions use."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from pydantic import JsonValue

if TYPE_CHECKING:
    from fledge.agents import Agent


@dataclass(frozen=True)
class WorkItem:
    external_id: str
    title: str
    body: str
    url: str
    repo_url: str


@dataclass(frozen=True)
class Comment:
    external_id: str
    author: str
    body: str


class TriggerCheckError(RuntimeError):
    """The trigger cannot do its job, e.g. its credentials lack a permission."""


@runtime_checkable
class Trigger(Protocol):
    async def check(self) -> None:
        """Raise TriggerCheckError if the trigger or its workflow cannot work.

        Called once at startup, before any polling.
        """
        ...

    async def poll(self) -> list[WorkItem]:
        """Work that currently qualifies. Returning an item again is harmless."""
        ...

    async def fetch(self, ref: str) -> WorkItem:
        """One specific item, for a manual launch."""
        ...

    async def poll_guidance(
        self, item_external_id: str, since: datetime
    ) -> list[Comment]:
        """Human comments on the item made after `since`."""
        ...


class TriggerSpec(Protocol):
    """A workflow's trigger as declared in workflow code.

    Declaring one must not do I/O or read secrets: workflow modules are also
    imported in step containers, which never start the trigger.
    """

    def start(self) -> Trigger:
        """Build the live trigger. Called once, in the control plane."""
        ...


@dataclass(frozen=True)
class Service:
    """A container started next to a step's container and removed with it.

    The step reaches it by `name`. With `ready_port` set, the step does not
    start until a TCP connection to that port succeeds.
    """

    name: str
    image: str
    env: Mapping[str, str] = field(default_factory=dict)
    ready_port: int | None = None


@dataclass(frozen=True)
class Sandbox:
    """What a step's container gets besides the image: services and environment."""

    services: Sequence[Service] = ()
    env: Mapping[str, str] = field(default_factory=dict)


NO_SANDBOX = Sandbox()


@dataclass(frozen=True)
class Repo:
    url: str
    path: Path
    """A directory inside the run's workspace."""
    branch: str
    """The run's working branch, created by clone_repo."""
    base: str
    """The repository's default branch."""


@dataclass(frozen=True)
class Run:
    """What a workflow function is called with."""

    id: UUID
    work: WorkItem


@dataclass(frozen=True)
class Workflow:
    name: str
    trigger: TriggerSpec
    fn: Callable[[Run], None]


@dataclass(frozen=True)
class Turn:
    prompt: str
    workdir: Path
    state_dir: Path
    """Where the harness keeps session state. It outlives the container, so a
    later turn in another container can resume the session."""
    session_id: str | None
    """None on an agent's first turn in a run."""
    output_schema: Mapping[str, JsonValue] | None = None
    """JSON Schema the agent's final answer must satisfy."""


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    final_message: str
    structured: JsonValue = None
    """The final answer as JSON when the turn had an output schema."""


@runtime_checkable
class Harness(Protocol):
    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        """Run the agent until it yields. Raise if the agent fails, or if
        `turn.session_id` cannot be resumed."""
        ...


class StepContext(Protocol):
    @property
    def run_id(self) -> UUID: ...

    @property
    def work(self) -> WorkItem: ...

    @property
    def workspace(self) -> Path:
        """Directory shared by all steps of the run."""
        ...

    def log(self, message: str) -> None: ...

    def agent_event(self, message: str) -> None: ...

    def claim_guidance(self) -> list[str]:
        """Undelivered guidance for the run. Claiming marks it delivered."""
        ...

    def agent(self, name: str) -> "Agent":
        """The agent of that name in the run's workflow code."""
        ...
