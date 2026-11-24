from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

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


class Runner(Protocol):
    """Runs STEP_COMMAND in a container with the run's workspace mounted at
    fledge.wire.WORKSPACE_PATH. Containers and workspaces are named from the
    ids alone, so they can be found again after a control-plane restart."""

    async def create_workspace(self, run_id: UUID) -> None: ...

    async def start_step(self, spec: StepSpec) -> None: ...

    async def step_state(self, step_run_id: UUID) -> ContainerState: ...

    async def stop_step(self, step_run_id: UUID) -> None:
        """Stop and remove the step's container. A missing container is fine."""
        ...

    async def delete_workspace(self, run_id: UUID) -> None: ...
