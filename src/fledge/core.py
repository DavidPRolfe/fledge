"""The types and interfaces that extensions implement."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID


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


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    final_message: str


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


@runtime_checkable
class Harness(Protocol):
    def run_turn(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        on_event: Callable[[str], None],
    ) -> TurnResult:
        """Run the agent until it yields. Raise if the agent fails.

        `session_id` is None on the first turn; pass the returned one back to
        continue the same conversation.
        """
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

    @property
    def outputs(self) -> Mapping[str, Mapping[str, str]]:
        """Outputs of earlier steps, by step name."""
        ...

    def log(self, message: str) -> None: ...

    def agent_event(self, message: str) -> None: ...

    def claim_guidance(self) -> list[str]:
        """Undelivered guidance for the run. Claiming marks it delivered."""
        ...

    def harness(self, name: str) -> Harness: ...


class Step(Protocol):
    @property
    def name(self) -> str: ...

    def run(self, ctx: StepContext) -> dict[str, str]:
        """Do the work. Return outputs for later steps. Raise to fail the run."""
        ...


@dataclass(frozen=True)
class Workflow:
    name: str
    trigger: TriggerSpec
    steps: Sequence[Step]

    def __post_init__(self) -> None:
        names = [step.name for step in self.steps]
        if len(set(names)) != len(names):
            raise ValueError(f"workflow {self.name!r} has duplicate step names")
