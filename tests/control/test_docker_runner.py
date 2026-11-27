"""Tests against a real Docker-compatible daemon at DOCKER_HOST."""

import gc
import io
import time
import uuid
import warnings
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import anyio
import docker
import pytest
from docker.errors import DockerException, NotFound

from fledge.control.runner.base import ContainerState, StepSpec
from fledge.control.runner.docker import (
    IMAGE_SOURCE_PATH,
    STEP_RUN_LABEL,
    DockerRunner,
    container_name,
    network_name,
    service_container_name,
    volume_name,
)
from fledge.core import Service
from fledge.wire import WORKSPACE_PATH

IMAGE = "docker.io/library/alpine:3.20"
# The step's shell script comes from its environment, so one runner command
# serves every test step.
SCRIPT_COMMAND = ("sh", "-c", 'eval "$STEP_SCRIPT"')
TIMEOUT_SECONDS = 30.0


@pytest.fixture(scope="module")
def client() -> Iterator[docker.DockerClient]:
    client: docker.DockerClient | str
    try:
        client = docker.from_env()
    except DockerException as err:
        client = str(err)
    if isinstance(client, str):
        # The SDK leaves the socket of a failed connect open. Collect it here,
        # once the exception holding it is gone and with its ResourceWarning
        # silenced, so a later collection under filterwarnings=error does not
        # turn this skip into an error.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        pytest.skip(f"no Docker-compatible daemon reachable at DOCKER_HOST: {client}")
    client.images.pull(IMAGE)
    yield client
    client.close()


@pytest.fixture(scope="module")
def network(client: docker.DockerClient) -> Iterator[str]:
    name = f"fledge-test-{uuid.uuid4().hex[:12]}"
    created = client.networks.create(name)
    yield name
    created.remove()


class Tracker:
    """Records the ids a test uses so teardown removes what the test left behind."""

    def __init__(self) -> None:
        self.run_ids: list[uuid.UUID] = []
        self.step_run_ids: list[uuid.UUID] = []

    def run_id(self) -> uuid.UUID:
        self.run_ids.append(uuid.uuid4())
        return self.run_ids[-1]

    def step_run_id(self) -> uuid.UUID:
        self.step_run_ids.append(uuid.uuid4())
        return self.step_run_ids[-1]


@pytest.fixture
def ids(client: docker.DockerClient) -> Iterator[Tracker]:
    tracker = Tracker()
    yield tracker
    for step_run_id in tracker.step_run_ids:
        with suppress(NotFound):
            client.containers.get(container_name(step_run_id)).remove(force=True)
        labelled = client.containers.list(
            all=True, filters={"label": f"{STEP_RUN_LABEL}={step_run_id}"}
        )
        for container in labelled:
            with suppress(NotFound):
                container.remove(force=True)
        with suppress(NotFound):
            client.networks.get(network_name(step_run_id)).remove()
    for run_id in tracker.run_ids:
        with suppress(NotFound):
            client.volumes.get(volume_name(run_id)).remove(force=True)


@pytest.fixture
def runner(network: str) -> DockerRunner:
    return DockerRunner(network, command=SCRIPT_COMMAND)


def spec(run_id: uuid.UUID, step_run_id: uuid.UUID, script: str) -> StepSpec:
    return StepSpec(
        step_run_id=step_run_id,
        run_id=run_id,
        image=IMAGE,
        env={"STEP_SCRIPT": script},
    )


async def wait_for_state(
    runner: DockerRunner, step_run_id: uuid.UUID, state: ContainerState
) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while (current := await runner.step_state(step_run_id)) != state:
        if time.monotonic() > deadline:
            pytest.fail(f"step stayed {current} instead of reaching {state}")
        await anyio.sleep(0.2)


def exit_code(client: docker.DockerClient, step_run_id: uuid.UUID) -> int:
    container = client.containers.get(container_name(step_run_id))
    code: int = container.attrs["State"]["ExitCode"]
    return code


@pytest.mark.anyio
async def test_steps_share_the_run_workspace(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker, network: str
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)

    waiter = ids.step_run_id()
    await runner.start_step(
        spec(run_id, waiter, "until [ -f go ]; do sleep 0.1; done; cat go > seen")
    )
    await wait_for_state(runner, waiter, ContainerState.RUNNING)

    releaser = ids.step_run_id()
    await runner.start_step(spec(run_id, releaser, "echo hello > go"))
    await wait_for_state(runner, releaser, ContainerState.EXITED)
    await wait_for_state(runner, waiter, ContainerState.EXITED)

    checker = ids.step_run_id()
    await runner.start_step(
        spec(run_id, checker, f'test "$(cat {WORKSPACE_PATH}/seen)" = hello')
    )
    await wait_for_state(runner, checker, ContainerState.EXITED)

    assert [exit_code(client, s) for s in (waiter, releaser, checker)] == [0, 0, 0]
    attached = client.containers.get(container_name(checker)).attrs["NetworkSettings"][
        "Networks"
    ]
    assert network in attached


@pytest.mark.anyio
async def test_workspaces_of_different_runs_are_separate(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker
) -> None:
    first_run, second_run = ids.run_id(), ids.run_id()
    await runner.create_workspace(first_run)
    await runner.create_workspace(second_run)

    writer = ids.step_run_id()
    await runner.start_step(spec(first_run, writer, "touch marker"))
    await wait_for_state(runner, writer, ContainerState.EXITED)
    reader = ids.step_run_id()
    await runner.start_step(spec(second_run, reader, "test ! -e marker"))
    await wait_for_state(runner, reader, ContainerState.EXITED)

    assert exit_code(client, writer) == 0
    assert exit_code(client, reader) == 0


@pytest.mark.anyio
async def test_stop_step_removes_a_running_container(
    runner: DockerRunner, ids: Tracker
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    await runner.start_step(spec(run_id, step_run_id, "sleep 300"))
    await wait_for_state(runner, step_run_id, ContainerState.RUNNING)

    started = time.monotonic()
    await runner.stop_step(step_run_id)

    assert await runner.step_state(step_run_id) == ContainerState.MISSING
    # Well under the daemon's 10 second kill timeout: SIGTERM reached the step.
    assert time.monotonic() - started < 5


@pytest.mark.anyio
async def test_stop_step_removes_an_exited_container(
    runner: DockerRunner, ids: Tracker
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    await runner.start_step(spec(run_id, step_run_id, "true"))
    await wait_for_state(runner, step_run_id, ContainerState.EXITED)

    await runner.stop_step(step_run_id)

    assert await runner.step_state(step_run_id) == ContainerState.MISSING


@pytest.mark.anyio
async def test_missing_step_is_reported_and_stopping_it_is_fine(
    runner: DockerRunner,
) -> None:
    step_run_id = uuid.uuid4()

    assert await runner.step_state(step_run_id) == ContainerState.MISSING
    await runner.stop_step(step_run_id)


@pytest.mark.anyio
async def test_delete_workspace_removes_the_volume_and_is_idempotent(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    client.volumes.get(volume_name(run_id))

    await runner.delete_workspace(run_id)
    await runner.delete_workspace(run_id)

    with pytest.raises(NotFound):
        client.volumes.get(volume_name(run_id))


@pytest.mark.anyio
async def test_source_dir_is_mounted_read_only_over_the_image_source(
    network: str, client: docker.DockerClient, ids: Tracker, tmp_path: Path
) -> None:
    (tmp_path / "marker").write_text("local")
    runner = DockerRunner(network, command=SCRIPT_COMMAND, source_dir=tmp_path)
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    script = (
        f'test "$(cat {IMAGE_SOURCE_PATH}/marker)" = local'
        f" && ! touch {IMAGE_SOURCE_PATH}/other"
    )

    await runner.start_step(spec(run_id, step_run_id, script))
    await wait_for_state(runner, step_run_id, ContainerState.EXITED)

    assert exit_code(client, step_run_id) == 0


SERVICE_PORT = 8080
# Services have no command of their own, so the test service image runs the
# script in its environment, the way SCRIPT_COMMAND does for steps.
SERVICE_DOCKERFILE = f"""\
FROM {IMAGE}
CMD ["sh", "-c", "eval \\"$SERVICE_SCRIPT\\""]
"""


@pytest.fixture(scope="module")
def service_image(client: docker.DockerClient) -> Iterator[str]:
    tag = f"fledge-test-service:{uuid.uuid4().hex[:12]}"
    client.images.build(fileobj=io.BytesIO(SERVICE_DOCKERFILE.encode()), tag=tag)
    yield tag
    client.images.remove(tag, force=True)


def tcp_service(name: str, image: str, body: str) -> Service:
    """A service sending `body` to every TCP connection on SERVICE_PORT."""
    script = f"while true; do echo {body} | nc -l -p {SERVICE_PORT}; done"
    return Service(name=name, image=image, env={"SERVICE_SCRIPT": script})


def fetch_script(host: str, expected: str) -> str:
    """Succeeds once `host` sends `expected`; retries because the runner does
    not wait for services to be ready."""
    return (
        "for i in $(seq 20); do"
        f' [ "$(nc -w 1 {host} {SERVICE_PORT} </dev/null)" = {expected} ]'
        " && exit 0; sleep 0.2; done; exit 1"
    )


def step_labelled(client: docker.DockerClient, step_run_id: uuid.UUID) -> list[str]:
    containers = client.containers.list(
        all=True, filters={"label": f"{STEP_RUN_LABEL}={step_run_id}"}
    )
    return [container.name or "" for container in containers]


@pytest.mark.anyio
async def test_step_reaches_its_service_by_name(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker, service_image: str
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    step = replace(
        spec(run_id, step_run_id, fetch_script("web", "hello")),
        services=[tcp_service("web", service_image, "hello")],
    )

    await runner.start_step(step)
    await wait_for_state(runner, step_run_id, ContainerState.EXITED)

    assert exit_code(client, step_run_id) == 0


@pytest.mark.anyio
async def test_service_name_beats_a_same_named_container_on_the_main_network(
    runner: DockerRunner,
    client: docker.DockerClient,
    ids: Tracker,
    service_image: str,
    network: str,
) -> None:
    shared_name = f"fledge-test-svc-{uuid.uuid4().hex[:12]}"
    decoy_service = tcp_service(shared_name, service_image, "decoy")
    decoy = client.containers.run(
        service_image,
        name=shared_name,
        environment=dict(decoy_service.env),
        network=network,
        detach=True,
    )
    try:
        run_id = ids.run_id()
        await runner.create_workspace(run_id)
        # A step without services reaches the decoy by that name, so the step
        # with a service of the same name would too unless its own one wins.
        plain = ids.step_run_id()
        await runner.start_step(spec(run_id, plain, fetch_script(shared_name, "decoy")))
        await wait_for_state(runner, plain, ContainerState.EXITED)
        with_service = ids.step_run_id()
        await runner.start_step(
            replace(
                spec(run_id, with_service, fetch_script(shared_name, "mine")),
                services=[tcp_service(shared_name, service_image, "mine")],
            )
        )
        await wait_for_state(runner, with_service, ContainerState.EXITED)

        assert [exit_code(client, s) for s in (plain, with_service)] == [0, 0]
    finally:
        decoy.remove(force=True)


@pytest.mark.anyio
async def test_stop_step_removes_the_step_its_services_and_its_network(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker, service_image: str
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    step = replace(
        spec(run_id, step_run_id, "sleep 300"),
        services=[
            tcp_service("first", service_image, "one"),
            tcp_service("second", service_image, "two"),
        ],
    )
    await runner.start_step(step)
    await wait_for_state(runner, step_run_id, ContainerState.RUNNING)
    assert len(step_labelled(client, step_run_id)) == 3

    await runner.stop_step(step_run_id)

    assert await runner.step_state(step_run_id) == ContainerState.MISSING
    assert step_labelled(client, step_run_id) == []
    with pytest.raises(NotFound):
        client.networks.get(network_name(step_run_id))


@pytest.mark.anyio
async def test_stop_step_cleans_up_a_half_started_step_and_is_idempotent(
    runner: DockerRunner, client: docker.DockerClient, ids: Tracker, service_image: str
) -> None:
    run_id = ids.run_id()
    await runner.create_workspace(run_id)
    step_run_id = ids.step_run_id()
    step = replace(
        spec(run_id, step_run_id, "true"),
        services=[
            tcp_service("good", service_image, "ok"),
            Service(name="broken", image="not a valid image reference"),
        ],
    )

    with pytest.raises(DockerException):
        await runner.start_step(step)
    assert step_labelled(client, step_run_id) == [
        service_container_name(step_run_id, "good")
    ]

    await runner.stop_step(step_run_id)
    await runner.stop_step(step_run_id)

    assert step_labelled(client, step_run_id) == []
    with pytest.raises(NotFound):
        client.networks.get(network_name(step_run_id))
