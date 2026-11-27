"""Runs one step inside a step container and reports back to the control plane."""

import logging
import os
import socket
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import JsonValue

from fledge import engine
from fledge.agents import Agent
from fledge.core import WorkItem
from fledge.harnesses.base import Harness
from fledge.wire import (
    API_URL_ENV,
    READY_PORTS_ENV,
    STEP_RUN_ID_ENV,
    STEP_TOKEN_ENV,
    WORKSPACE_PATH,
    EventIn,
    EventsIn,
    GuidanceClaim,
    StepResult,
    StepRunContext,
)
from fledge.workflows import load_workflows

logger = logging.getLogger(__name__)

# Long enough to ride out a control-plane restart, short enough that a control
# plane that is really gone fails the step well inside the step timeout.
RETRY_WINDOW_SECONDS = 120.0
RETRY_INTERVAL_SECONDS = 2.0

# Services start next to the step's container; a database image can take tens
# of seconds before it accepts connections.
READY_TIMEOUT_SECONDS = 60.0
READY_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class RetryPolicy:
    window: float = RETRY_WINDOW_SECONDS
    interval: float = RETRY_INTERVAL_SECONDS


@dataclass(frozen=True)
class ReadyPolicy:
    timeout: float = READY_TIMEOUT_SECONDS
    interval: float = READY_INTERVAL_SECONDS


class ControlPlane:
    """Client for the internal API, scoped to one step run."""

    def __init__(
        self,
        client: httpx.Client,
        step_run_id: UUID,
        retry: RetryPolicy = RetryPolicy(),
    ) -> None:
        self._client = client
        self._base = f"/internal/step-runs/{step_run_id}"
        self._retry = retry

    def context(self) -> StepRunContext:
        response = self._request("GET", "")
        return StepRunContext.model_validate(response.json())

    def post_event(self, kind: Literal["log", "agent"], message: str) -> None:
        body = EventsIn(events=[EventIn(kind=kind, message=message)])
        self._request("POST", "/events", body.model_dump(mode="json"))

    def claim_guidance(self) -> list[str]:
        response = self._request("POST", "/guidance/claim")
        return GuidanceClaim.model_validate(response.json()).bodies

    def report(self, result: StepResult) -> None:
        self._request("POST", "/result", result.model_dump(mode="json"))

    def _request(
        self, method: str, path: str, body: object | None = None
    ) -> httpx.Response:
        # Only connection failures are retried, not read errors or timeouts: a
        # request that may have reached the server (a guidance claim, say) is
        # not safe to send twice. A 5xx means the server did not complete it.
        deadline = time.monotonic() + self._retry.window
        while True:
            try:
                response = self._client.request(method, self._base + path, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if time.monotonic() >= deadline:
                    raise
                logger.warning("control plane unreachable, retrying", exc_info=True)
            else:
                if not response.is_server_error or time.monotonic() >= deadline:
                    response.raise_for_status()
                    return response
                logger.warning(
                    "control plane returned %d, retrying", response.status_code
                )
            time.sleep(self._retry.interval)


@dataclass(frozen=True)
class ExecutorContext:
    """The StepContext a step sees inside its container."""

    run_id: UUID
    work: WorkItem
    workspace: Path
    control_plane: ControlPlane
    agents: Mapping[str, Agent]

    def log(self, message: str) -> None:
        self.control_plane.post_event("log", message)

    def agent_event(self, message: str) -> None:
        self.control_plane.post_event("agent", message)

    def claim_guidance(self) -> list[str]:
        return self.control_plane.claim_guidance()

    def harness(self, agent: str) -> Harness:
        if agent not in self.agents:
            raise LookupError(f"the run's workflow code defines no agent {agent!r}")
        return self.agents[agent].harness


def wait_for_services(addresses: Sequence[str], policy: ReadyPolicy) -> None:
    """Wait until each `host:port` accepts a TCP connection, all within one timeout."""
    deadline = time.monotonic() + policy.timeout
    for address in addresses:
        host, _, port = address.rpartition(":")
        while True:
            try:
                with socket.create_connection((host, int(port)), policy.interval):
                    break
            except OSError as err:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"service {address} did not accept a connection within "
                        f"{policy.timeout:g} seconds"
                    ) from err
            time.sleep(policy.interval)


def _run_step(
    run: StepRunContext,
    control_plane: ControlPlane,
    workspace: Path,
    ready: ReadyPolicy,
) -> JsonValue:
    # Loading the snapshot is what makes its package importable, so it has to
    # happen before the step's function is resolved.
    loaded = load_workflows(run.workflow_files)
    step = engine.resolve_step(run.function)
    ctx = ExecutorContext(
        run_id=run.run_id,
        work=run.work,
        workspace=workspace,
        control_plane=control_plane,
        agents=loaded.agents,
    )
    ready_ports = os.environ.get(READY_PORTS_ENV)
    if ready_ports:
        wait_for_services(ready_ports.split(","), ready)
    return step.execute(ctx, run.args)


def execute(
    control_plane: ControlPlane,
    workspace: Path = Path(WORKSPACE_PATH),
    ready: ReadyPolicy = ReadyPolicy(),
) -> int:
    """Run the step and report its result. Returns the process exit code."""
    run = control_plane.context()
    # Anything the step raises must reach the control plane as a failed result;
    # a container that just exits leaves the run waiting until the step timeout.
    try:
        result = _run_step(run, control_plane, workspace, ready)
    except Exception as err:
        logger.exception("step failed")
        control_plane.report(
            StepResult(status="failed", error=f"{type(err).__name__}: {err}")
        )
        return 1
    control_plane.report(StepResult(status="succeeded", result=result))
    return 0


def execute_from_env() -> int:
    """Entry point for `fledge step-exec`."""
    step_run_id = UUID(os.environ[STEP_RUN_ID_ENV])
    headers = {"Authorization": f"Bearer {os.environ[STEP_TOKEN_ENV]}"}
    with httpx.Client(base_url=os.environ[API_URL_ENV], headers=headers) as client:
        return execute(ControlPlane(client, step_run_id))
