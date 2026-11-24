import asyncio
from collections.abc import Sequence
from uuid import UUID

import docker
from docker.errors import NotFound

from fledge.runner.base import STEP_COMMAND, ContainerState, StepSpec
from fledge.wire import WORKSPACE_PATH

# "created" counts as running: start_step creates and starts in one call, so a
# created container is a step still on its way up, not one that has finished.
_RUNNING_STATUSES = frozenset({"created", "running", "restarting", "paused"})
_EXITED_STATUSES = frozenset({"exited", "dead"})


def volume_name(run_id: UUID) -> str:
    return f"fledge-run-{run_id}"


def container_name(step_run_id: UUID) -> str:
    return f"fledge-step-{step_run_id}"


class DockerRunner:
    """Runner backed by the Docker daemon at DOCKER_HOST (or the default socket)."""

    def __init__(self, network: str, command: Sequence[str] = STEP_COMMAND) -> None:
        self._client = docker.from_env()
        self._network = network
        self._command = list(command)

    async def create_workspace(self, run_id: UUID) -> None:
        await asyncio.to_thread(self._client.volumes.create, name=volume_name(run_id))

    async def start_step(self, spec: StepSpec) -> None:
        await asyncio.to_thread(
            self._client.containers.run,
            spec.image,
            self._command,
            name=container_name(spec.step_run_id),
            environment=dict(spec.env),
            volumes={volume_name(spec.run_id): {"bind": WORKSPACE_PATH, "mode": "rw"}},
            working_dir=WORKSPACE_PATH,
            network=self._network,
            detach=True,
            # The step command runs as PID 1, which ignores SIGTERM unless it
            # installs a handler; an init process forwards it so stop_step is
            # graceful instead of waiting out the kill timeout.
            init=True,
        )

    async def step_state(self, step_run_id: UUID) -> ContainerState:
        try:
            container = await asyncio.to_thread(
                self._client.containers.get, container_name(step_run_id)
            )
        except NotFound:
            return ContainerState.MISSING
        status = container.status
        if status in _RUNNING_STATUSES:
            return ContainerState.RUNNING
        if status in _EXITED_STATUSES:
            return ContainerState.EXITED
        raise ValueError(
            f"unexpected container status {status!r} for step {step_run_id}"
        )

    async def stop_step(self, step_run_id: UUID) -> None:
        await asyncio.to_thread(self._stop_and_remove, container_name(step_run_id))

    def _stop_and_remove(self, name: str) -> None:
        try:
            container = self._client.containers.get(name)
            container.stop()
            container.remove()
        except NotFound:
            return

    async def delete_workspace(self, run_id: UUID) -> None:
        await asyncio.to_thread(self._remove_volume, volume_name(run_id))

    def _remove_volume(self, name: str) -> None:
        # Cancel and normal run cleanup can both delete the same workspace, and
        # a restart can repeat a deletion that already happened. Either way the
        # goal (no volume) is met, so a missing volume is success. A volume that
        # is still in use is not caught: that means a step container survived,
        # which the caller must see.
        try:
            volume = self._client.volumes.get(name)
            volume.remove()
        except NotFound:
            return
