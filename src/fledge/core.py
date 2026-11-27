"""The types and interfaces that workflow code and extensions use."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fledge.harnesses.base import Harness


@dataclass(frozen=True)
class WorkItem:
    external_id: str
    title: str
    body: str
    url: str
    repo_url: str


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

    def harness(self, agent: str) -> Harness:
        """The harness of the agent of that name in the run's workflow code."""
        ...
