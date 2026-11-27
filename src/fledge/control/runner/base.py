from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from fledge.core import Service

STEP_COMMAND = ("fledge", "step-exec")


class ContainerState(Enum):
    RUNNING = "running"
    EXITED = "exited"
    MISSING = "missing"


@dataclass(frozen=True)
class StepSpec:
    step_run_id: UUID
    run_id: UUID
    image: str
    env: Mapping[str, str]
    services: Sequence[Service] = ()


class Runner(Protocol):
    """Runs STEP_COMMAND in a container with the run's workspace mounted at
    fledge.wire.WORKSPACE_PATH. Containers and workspaces are named from the
    ids alone, so they can be found again after a control-plane restart."""

    async def create_workspace(self, run_id: UUID) -> None: ...

    async def start_step(self, spec: StepSpec) -> None:
        """Start the step's container, with the spec's services next to it.

        The step reaches each service by its name. Waiting for a service's
        ready_port is the step container's job, not the runner's.
        """
        ...

    async def step_state(self, step_run_id: UUID) -> ContainerState: ...

    async def stop_step(self, step_run_id: UUID) -> None:
        """Stop and remove the step's container and its services. Missing
        containers are fine."""
        ...

    async def delete_workspace(self, run_id: UUID) -> None: ...
