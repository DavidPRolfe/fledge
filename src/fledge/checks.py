"""Checks, and the `until` gate that loops on their feedback."""

import shlex
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Concatenate, Literal, Protocol, overload

from pydantic import Field, JsonValue, TypeAdapter

from fledge import engine
from fledge.core import NO_SANDBOX, Repo, Run, Sandbox, StepContext
from fledge.engine import RunStopped, Step

OUTPUT_TAIL_LINES = 100


@dataclass(frozen=True)
class FeedbackItem:
    author: str
    body: str


# The `kind` fields let a verdict be told apart once it is stored as JSON.
@dataclass(frozen=True)
class Passed:
    kind: Literal["passed"] = "passed"


@dataclass(frozen=True)
class Feedback:
    items: Sequence[FeedbackItem]
    kind: Literal["feedback"] = "feedback"


@dataclass(frozen=True)
class Pending:
    kind: Literal["pending"] = "pending"


@dataclass(frozen=True)
class Stopped:
    kind: Literal["stopped"] = "stopped"


Verdict = Passed | Feedback | Pending | Stopped
Settled = Passed | Feedback | Stopped

_SETTLED: TypeAdapter[Settled] = TypeAdapter(
    Annotated[Settled, Field(discriminator="kind")]
)


class RoundsExhausted(Exception):  # noqa: N818 - named for what a workflow author catches
    """A check still had feedback after the last round `until` allows."""


class Check(Protocol):
    def evaluate(self, since: datetime) -> Settled:
        """Evaluate once, from a workflow. `since` is when the surrounding
        `until` loop last received feedback, or when it started."""
        ...


def until(check: Check, *, max_rounds: int) -> Iterator[Feedback]:
    """Yield the check's feedback until it passes.

    Raises RoundsExhausted if it still has feedback after `max_rounds` rounds,
    and RunStopped if the check says the run should stop.
    """
    since = engine.clock()
    rounds = 0
    while True:
        verdict = check.evaluate(since)
        if isinstance(verdict, Passed):
            return
        if isinstance(verdict, Stopped):
            raise RunStopped
        if rounds == max_rounds:
            raise RoundsExhausted(f"still not passing after {max_rounds} rounds")
        rounds += 1
        since = engine.clock()
        yield verdict


def render_feedback(feedback: Feedback) -> str:
    """Feedback as a prompt for an agent."""
    findings = "\n\n".join(
        f"From {item.author}:\n{item.body}" for item in feedback.items
    )
    return "Address these findings, then summarise what you did.\n\n" + findings


def guidance_verdict(bodies: Sequence[str]) -> JsonValue:
    """What a wait records when a person sent guidance while the run was in it."""
    feedback = Feedback(
        [FeedbackItem("a person overseeing this work", b) for b in bodies]
    )
    dumped: JsonValue = _SETTLED.dump_python(feedback, mode="json")
    return dumped


@dataclass(frozen=True)
class _StepCheck:
    run: Callable[[], Passed | Feedback]

    def evaluate(self, since: datetime) -> Settled:
        del since
        return self.run()


@dataclass(frozen=True)
class ContainerCheck[**P]:
    """What `@check` makes: call it with the check's arguments to get a Check."""

    step: Step[P, Passed | Feedback]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Check:
        return _StepCheck(lambda: self.step(*args, **kwargs))

    def within(self, sandbox: Sandbox) -> "ContainerCheck[P]":
        """The same check, run with another sandbox."""
        return ContainerCheck(Step(self.step.fn, sandbox))


@overload
def check[**P](
    fn: Callable[Concatenate[StepContext, P], Passed | Feedback], /
) -> ContainerCheck[P]: ...


@overload
def check[**P](
    *, sandbox: Sandbox
) -> Callable[
    [Callable[Concatenate[StepContext, P], Passed | Feedback]], ContainerCheck[P]
]: ...


def check[**P](
    fn: Callable[Concatenate[StepContext, P], Passed | Feedback] | None = None,
    /,
    *,
    sandbox: Sandbox = NO_SANDBOX,
) -> (
    ContainerCheck[P]
    | Callable[
        [Callable[Concatenate[StepContext, P], Passed | Feedback]], ContainerCheck[P]
    ]
):
    """Make a function a check that runs in a container, like a step."""
    if fn is not None:
        return ContainerCheck(Step(fn))
    return lambda decorated: ContainerCheck(Step(decorated, sandbox))


@dataclass(frozen=True)
class WaitContext:
    run: Run
    since: datetime
    """When the surrounding `until` loop last received feedback, or started."""
    started_at: datetime
    """When this wait began. Later than `since` by however long the loop
    body took to act on the feedback."""


@dataclass(frozen=True)
class _WaitCheck:
    fn: Callable[..., Verdict]
    args: tuple[object, ...]
    kwargs: dict[str, object]
    poll_interval: timedelta | None

    def evaluate(self, since: datetime) -> Settled:
        run = engine.current_run()

        def poll(started_at: datetime) -> JsonValue | None:
            ctx = WaitContext(run, since, started_at)
            verdict = self.fn(ctx, *self.args, **self.kwargs)
            if isinstance(verdict, Pending):
                return None
            dumped: JsonValue = _SETTLED.dump_python(verdict, mode="json")
            return dumped

        recorded = {
            **engine.dump_args(self.fn, self.args, self.kwargs),
            "since": since.isoformat(),
        }
        result = engine.record(
            "wait",
            self.fn.__name__,
            engine.ref(self.fn),
            recorded,
            poll=poll,
            poll_interval=self.poll_interval,
        )
        return _SETTLED.validate_python(result)


type WaitFunction[**P] = Callable[Concatenate[WaitContext, P], Verdict]


@overload
def wait[**P](fn: WaitFunction[P], /) -> Callable[P, Check]: ...


@overload
def wait[**P](
    *, poll_interval: timedelta
) -> Callable[[WaitFunction[P]], Callable[P, Check]]: ...


def wait[**P](
    fn: WaitFunction[P] | None = None,
    /,
    *,
    poll_interval: timedelta | None = None,
) -> Callable[P, Check] | Callable[[WaitFunction[P]], Callable[P, Check]]:
    """Make a function a check that the control plane polls, with no container.

    The function takes a WaitContext first and is called again each poll
    until it returns something other than Pending. It may do I/O. It is polled
    every `poll_interval` if given, else every FLEDGE_POLL_INTERVAL_SECONDS.
    """

    def decorate(decorated: WaitFunction[P]) -> Callable[P, Check]:
        engine.ref(decorated)

        def build(*args: P.args, **kwargs: P.kwargs) -> Check:
            return _WaitCheck(decorated, args, kwargs, poll_interval)

        return build

    if fn is not None:
        return decorate(fn)
    return decorate


@check
def command(ctx: StepContext, argv: list[str], repo: Repo) -> Passed | Feedback:
    """Passes when `argv` exits with status 0 in the repository."""
    ctx.log(f"running {shlex.join(argv)} in {repo.path}")
    result = subprocess.run(  # noqa: S603 - argv comes from workflow code and runs without a shell
        argv,
        cwd=repo.path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0:
        return Passed()
    tail = "\n".join(result.stdout.splitlines()[-OUTPUT_TAIL_LINES:])
    body = (
        f"`{shlex.join(argv)}` exited with status {result.returncode}.\n\n"
        f"End of its output:\n{tail}"
    )
    return Feedback([FeedbackItem(argv[0], body)])
