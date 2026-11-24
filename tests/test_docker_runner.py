"""Tests against a real Docker-compatible daemon at DOCKER_HOST."""

import gc
import time
import uuid
import warnings
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import anyio
import docker
import pytest
from docker.errors import DockerException, NotFound

from fledge.runner.base import ContainerState, StepSpec
from fledge.runner.docker import (
    IMAGE_SOURCE_PATH,
    DockerRunner,
    container_name,
    volume_name,
)
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
