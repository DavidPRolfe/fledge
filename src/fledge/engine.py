"""Runs workflow functions by replay.

A workflow function is called from the top every time its run can advance.
Each step or wait it calls is looked up in the run's journal by position:
a call with a recorded result returns it, and the first call without one
suspends the function by raising through it. Nothing of a suspended run is
kept in memory.
"""

import importlib
import inspect
import logging
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Concatenate, Literal, Protocol, overload
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from fledge.core import NO_SANDBOX, Run, Sandbox, StepContext
from fledge.triggers import TriggerSpec

logger = logging.getLogger(__name__)

CallKind = Literal["step", "wait"]


class StepFailed(Exception):  # noqa: N818 - named for what a workflow author catches
    """A step raised in its container, timed out, or its container died."""


class NondeterminismError(Exception):
    """A replay reached a different call than the one recorded at that position."""


class RunStopped(BaseException):
    """Ends the run as cancelled.

    A BaseException so that `except Exception` in a workflow cannot swallow it.
    """


@dataclass(frozen=True)
class Workflow:
    name: str
    trigger: TriggerSpec
    fn: Callable[[Run], None]


class JournalEntry(Protocol):
    """One recorded call of a run, as the engine reads it from the journal."""

    @property
    def id(self) -> UUID: ...

    @property
    def function(self) -> str: ...

    @property
    def status(self) -> str:
        """`running`, `succeeded`, `failed` or `cancelled`."""
        ...

    @property
    def result(self) -> JsonValue: ...

    @property
    def error(self) -> str | None: ...

    @property
    def started_at(self) -> datetime: ...

    @property
    def finished_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class PendingCall:
    """The call a suspended run is waiting on."""

    position: int
    kind: CallKind
    name: str
    function: str
    args: dict[str, JsonValue]
    sandbox: Sandbox
    entry: JournalEntry | None
    """The journal row if the call has already been started."""
    poll: Callable[[datetime], JsonValue | None] | None
    """For a wait: evaluates it once, given when the wait began. None means
    it is still pending."""
    poll_interval: timedelta | None = None
    """For a wait: how often to poll it, if not the control plane's interval."""


class _Suspend(BaseException):
    def __init__(self, call: PendingCall) -> None:
        super().__init__()
        self.call = call


@dataclass(frozen=True)
class Succeeded:
    pass


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class Cancelled:
    pass


@dataclass(frozen=True)
class Suspended:
    call: PendingCall


Outcome = Succeeded | Failed | Cancelled | Suspended


@dataclass
class _Replay:
    run: Run
    journal: Sequence[JournalEntry]
    clock: datetime
    cursor: int = 0


_current: ContextVar[_Replay] = ContextVar("fledge_replay")


def _state() -> _Replay:
    state = _current.get(None)
    if state is None:
        raise RuntimeError("steps, agents and checks can only be called in a workflow")
    return state


def replay(
    workflow: Workflow,
    run: Run,
    started_at: datetime,
    journal: Sequence[JournalEntry],
) -> Outcome:
    """Call the workflow function against the journal, which is in call order."""
    token = _current.set(_Replay(run, journal, started_at))
    try:
        workflow.fn(run)
    except _Suspend as suspend:
        return Suspended(suspend.call)
    except RunStopped:
        return Cancelled()
    except Exception as err:
        # Workflow code failing is an outcome of the run, not of the engine.
        logger.exception("workflow %s failed in run %s", workflow.name, run.id)
        return Failed(f"{type(err).__name__}: {err}")
    finally:
        _current.reset(token)
    return Succeeded()


def current_run() -> Run:
    return _state().run


def clock() -> datetime:
    """When the latest call the workflow has passed finished, or the run started.

    Workflow code must not read the real clock; this is the same on every replay.
    """
    return _state().clock


def record(  # noqa: PLR0913 - one argument per journal column
    kind: CallKind,
    name: str,
    function: str,
    args: dict[str, JsonValue],
    *,
    sandbox: Sandbox = NO_SANDBOX,
    poll: Callable[[datetime], JsonValue | None] | None = None,
    poll_interval: timedelta | None = None,
) -> JsonValue:
    """The recorded result of the workflow's next call, or suspend the run on it."""
    state = _state()
    position = state.cursor
    state.cursor += 1
    entry = state.journal[position] if position < len(state.journal) else None
    if entry is not None:
        if entry.function != function:
            raise NondeterminismError(
                f"call {position} was recorded as {entry.function} but the workflow "
                f"now calls {function}; workflow code must be deterministic"
            )
        if entry.finished_at is not None:
            state.clock = entry.finished_at
        if entry.status == "succeeded":
            return entry.result
        if entry.status == "failed":
            raise StepFailed(entry.error or f"{name} failed")
    raise _Suspend(
        PendingCall(
            position, kind, name, function, args, sandbox, entry, poll, poll_interval
        )
    )


def ref(obj: Callable[..., object] | type) -> str:
    """Where a module-level function or class can be imported from."""
    if "<locals>" in obj.__qualname__:
        raise TypeError(
            f"{obj.__qualname__} must be defined at module level, so that a "
            "step container can import it"
        )
    return f"{obj.__module__}:{obj.__qualname__}"


def resolve(reference: str) -> object:
    module_name, _, qualname = reference.partition(":")
    obj: object = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def resolve_type(reference: str) -> type:
    obj = resolve(reference)
    if not isinstance(obj, type):
        raise TypeError(f"{reference} is not a class")
    return obj


def dump_args(
    fn: Callable[..., object], args: tuple[object, ...], kwargs: dict[str, object]
) -> dict[str, JsonValue]:
    """The call's arguments after `fn`'s context parameter, as JSON by name."""
    signature = inspect.signature(fn, eval_str=True)
    bound = signature.bind(None, *args, **kwargs)
    bound.apply_defaults()
    context, *names = bound.arguments
    del context
    dumped: dict[str, JsonValue] = {}
    for name in names:
        adapter: TypeAdapter[object] = TypeAdapter(
            signature.parameters[name].annotation
        )
        dumped[name] = adapter.dump_python(bound.arguments[name], mode="json")
    return dumped


def _check_signature(fn: Callable[..., object]) -> None:
    signature = inspect.signature(fn, eval_str=True)
    context, *parameters = signature.parameters.values()
    del context
    for parameter in parameters:
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            raise TypeError(f"{fn.__qualname__} cannot take *args or **kwargs")
        if parameter.annotation is parameter.empty:
            raise TypeError(
                f"{fn.__qualname__} parameter {parameter.name!r} needs a type annotation"
            )
    if signature.return_annotation is signature.empty:
        raise TypeError(f"{fn.__qualname__} needs a return annotation")


@dataclass(frozen=True)
class Step[**P, R]:
    """A function that runs in its own container when a workflow calls it."""

    fn: Callable[Concatenate[StepContext, P], R]
    sandbox: Sandbox = NO_SANDBOX

    def __post_init__(self) -> None:
        ref(self.fn)
        _check_signature(self.fn)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.call_as(self.fn.__name__, self.sandbox, *args, **kwargs)

    def call_as(
        self, name: str, sandbox: Sandbox, /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Call the step under another display name and sandbox."""
        result = record(
            "step",
            name,
            ref(self.fn),
            dump_args(self.fn, args, kwargs),
            sandbox=sandbox,
        )
        return self._result_adapter().validate_python(result)

    def execute(self, ctx: StepContext, args: dict[str, JsonValue]) -> JsonValue:
        """Run the function in this process, from and to the journal's JSON."""
        signature = inspect.signature(self.fn, eval_str=True)
        kwargs = {
            name: TypeAdapter(signature.parameters[name].annotation).validate_python(
                value
            )
            for name, value in args.items()
        }
        result = self.fn(ctx, **kwargs)  # type: ignore[call-arg]  # kwargs are built from the signature at run time, which P cannot express
        dumped: JsonValue = self._result_adapter().dump_python(result, mode="json")
        return dumped

    def _result_adapter(self) -> TypeAdapter[R]:
        signature = inspect.signature(self.fn, eval_str=True)
        return TypeAdapter(signature.return_annotation)


def resolve_step(reference: str) -> Step[..., object]:
    obj = resolve(reference)
    # A @check is called to build a check, and carries its step.
    candidate = getattr(obj, "step", obj)
    if not isinstance(candidate, Step):
        raise TypeError(f"{reference} is not a step")
    return candidate


@overload
def step[**P, R](fn: Callable[Concatenate[StepContext, P], R], /) -> Step[P, R]: ...


@overload
def step[**P, R](
    *, sandbox: Sandbox
) -> Callable[[Callable[Concatenate[StepContext, P], R]], Step[P, R]]: ...


def step[**P, R](
    fn: Callable[Concatenate[StepContext, P], R] | None = None,
    /,
    *,
    sandbox: Sandbox = NO_SANDBOX,
) -> Step[P, R] | Callable[[Callable[Concatenate[StepContext, P], R]], Step[P, R]]:
    """Make a function a step: `@step` or `@step(sandbox=...)`.

    The function takes a StepContext first. A workflow calls it without one.
    """
    if fn is not None:
        return Step(fn)
    return lambda decorated: Step(decorated, sandbox)


def workflow(*, trigger: TriggerSpec) -> Callable[[Callable[[Run], None]], Workflow]:
    """Make a function a workflow, named after the function."""
    return lambda fn: Workflow(name=fn.__name__, trigger=trigger, fn=fn)
