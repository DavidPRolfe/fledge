import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import docker
from docker.errors import NotFound
from docker.models.containers import Container
from docker.models.networks import Network

from fledge.core import Service
from fledge.runner.base import STEP_COMMAND, ContainerState, StepSpec
from fledge.wire import WORKSPACE_PATH

# "created" counts as running: start_step creates a container and starts it
# straight away, so a created container is a step still on its way up, not one
# that has finished.
_RUNNING_STATUSES = frozenset({"created", "running", "restarting", "paused"})
_EXITED_STATUSES = frozenset({"exited", "dead"})
# Where the Dockerfile copies src/. The image installs fledge in editable mode,
# so code mounted here replaces the code the image was built with.
IMAGE_SOURCE_PATH = "/app/src"
# Every container and network of a step carries this label, so stop_step finds
# the step's services after a control-plane restart without knowing their names.
STEP_RUN_LABEL = "fledge.step-run"


def volume_name(run_id: UUID) -> str:
    return f"fledge-run-{run_id}"


def container_name(step_run_id: UUID) -> str:
    return f"fledge-step-{step_run_id}"


def network_name(step_run_id: UUID) -> str:
    return f"fledge-step-{step_run_id}-net"


def service_container_name(step_run_id: UUID, service: str) -> str:
    return f"fledge-step-{step_run_id}-svc-{service}"


class DockerRunner:
    """Runner backed by the Docker daemon at DOCKER_HOST (or the default socket)."""

    def __init__(
        self,
        network: str,
        command: Sequence[str] = STEP_COMMAND,
        source_dir: Path | None = None,
    ) -> None:
        self._client = docker.from_env()
        self._network = network
        self._command = list(command)
        self._source_dir = source_dir

    async def create_workspace(self, run_id: UUID) -> None:
        await asyncio.to_thread(self._client.volumes.create, name=volume_name(run_id))

    async def start_step(self, spec: StepSpec) -> None:
        await asyncio.to_thread(self._start_step, spec)

    def _start_step(self, spec: StepSpec) -> None:
        labels = {STEP_RUN_LABEL: str(spec.step_run_id)}
        hosts: dict[str, str] = {}
        step_network: Network | None = None
        if spec.services:
            step_network = self._client.networks.create(
                network_name(spec.step_run_id), labels=labels
            )
            hosts = {
                service.name: self._start_service(spec.step_run_id, service, labels)
                for service in spec.services
            }
        container = self._client.containers.create(
            spec.image,
            self._command,
            name=container_name(spec.step_run_id),
            environment=dict(spec.env),
            volumes=self._step_volumes(spec.run_id),
            working_dir=WORKSPACE_PATH,
            network=self._network,
            labels=labels,
            # The step is on two networks, and a service name such as
            # "postgres" may also name a container on the control plane's
            # network (the bundled compose file calls Fledge's own database
            # that). /etc/hosts entries win over Docker's DNS, so the step
            # always reaches its own service, never the control plane's.
            extra_hosts=hosts or None,
            # The step command runs as PID 1, which ignores SIGTERM unless it
            # installs a handler; an init process forwards it so stop_step is
            # graceful instead of waiting out the kill timeout.
            init=True,
        )
        if step_network is not None:
            step_network.connect(container)
        container.start()

    def _step_volumes(self, run_id: UUID) -> dict[str, dict[str, str]]:
        volumes = {volume_name(run_id): {"bind": WORKSPACE_PATH, "mode": "rw"}}
        if self._source_dir is not None:
            volumes[str(self._source_dir.resolve())] = {
                "bind": IMAGE_SOURCE_PATH,
                "mode": "ro",
            }
        return volumes

    def _start_service(
        self, step_run_id: UUID, service: Service, labels: Mapping[str, str]
    ) -> str:
        """Start the service on the step's own network and return its address there."""
        step_network = network_name(step_run_id)
        container = self._client.containers.run(
            service.image,
            name=service_container_name(step_run_id, service.name),
            environment=dict(service.env),
            network=step_network,
            labels=dict(labels),
            detach=True,
        )
        container.reload()
        address: str = container.attrs["NetworkSettings"]["Networks"][step_network][
            "IPAddress"
        ]
        return address

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
        await asyncio.to_thread(self._stop_step, step_run_id)

    def _stop_step(self, step_run_id: UUID) -> None:
        # The step container goes first: it is attached to the step's network,
        # which cannot be removed while any container is still on it.
        self._stop_and_remove(container_name(step_run_id))
        services = self._client.containers.list(
            all=True, filters={"label": f"{STEP_RUN_LABEL}={step_run_id}"}
        )
        for service in services:
            self._remove_container(service)
        self._remove_network(network_name(step_run_id))

    def _stop_and_remove(self, name: str) -> None:
        try:
            container = self._client.containers.get(name)
        except NotFound:
            return
        self._remove_container(container)

    def _remove_container(self, container: Container) -> None:
        try:
            container.stop()
            container.remove()
        except NotFound:
            return

    def _remove_network(self, name: str) -> None:
        try:
            network = self._client.networks.get(name)
            network.remove()
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
