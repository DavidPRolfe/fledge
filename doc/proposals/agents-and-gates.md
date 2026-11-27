# Proposal: workflows as functions, agents and sandboxes

Status: proposed. This covers the API a workflow author sees. Agent calls
(section 3) and pull request handling (section 6) are the least settled parts.

## Problem

The workflow we most want to run is one implementer agent taking a ticket to a
merged pull request, reacting to feedback on the way:

1. An implementer agent works on the ticket in a container that can run the
   project, including the services it needs (for Fledge itself, Postgres).
2. A reviewer agent on a different harness reviews the change. The two go
   back and forth until the reviewer approves.
3. A draft pull request is opened. The implementer fixes CI failures until
   the checks pass, and the pull request is marked ready for review.
4. The implementer responds to human review until the pull request is merged,
   optionally passing each response through the reviewer agent first.

Today a run ends when the pull request opens, steps run once each in order,
an agent's session is lost when its step ends, and a step container cannot
have a service such as Postgres next to it.

## Principles

1. **A run is the life of one change**, from ticket to merged or closed pull
   request.
2. **A workflow is a plain Python function that really executes.** Loops and
   branches are Python.
3. **Steps hand each other values**, as arguments and return values.
4. **An agent is a value with identity.** It has a harness, a sandbox and a
   session that lasts the run. Any number of calls can prompt it.
5. **A sandbox is rebuilt each time it is entered.** An agent's files and
   session persist. Its container and services do not.
6. **Work happens in containers; waiting and deciding happen in the control
   plane.** A run waiting on CI or a person holds no container.
7. **Agents do the git work; the workflow says when.** Committing, merging in
   other people's commits, pushing and writing the pull request are the
   agent's job. The workflow says when to publish, and Fledge verifies that
   it happened.

## What a user writes

```python
# workflows/fledge.py
from fledge import Agent, Repo, Run, Sandbox, Service, until, workflow
from fledge.github import (
    GitHubIssues,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    push,
)
from fledge.harnesses import ClaudeCode, Codex
from fledge.steps import clone_repo

IMPLEMENT = """..."""  # formatted with the work item
REVIEW = """..."""

dev = Sandbox(
    services=[
        Service(
            name="postgres",
            image="postgres:17",
            env={"POSTGRES_PASSWORD": "fledge"},
            ready_port=5432,
        )
    ],
    env={
        "FLEDGE_TEST_DATABASE_URL": "postgresql://postgres:fledge@postgres/postgres"
    },
)

implementer = Agent("implementer", ClaudeCode(), sandbox=dev)
reviewer = Agent("reviewer", Codex())


def agent_review(repo: Repo) -> None:
    for feedback in until(reviewer.approves(REVIEW, repo), max_rounds=3):
        implementer.prompt(feedback, repo)


@workflow(trigger=GitHubIssues(repo="DavidPRolfe/fledge", label="fledge"))
def fledge(run: Run) -> None:
    repo = clone_repo(run.work.repo_url)
    implementer.prompt(IMPLEMENT.format(work=run.work), repo)
    agent_review(repo)

    pr = open_pull_request(implementer, repo, draft=True)
    for feedback in until(checks_green(pr), max_rounds=5):
        implementer.prompt(feedback, repo)
        push(implementer, pr)
    mark_ready(pr)

    for feedback in until(merged(pr), max_rounds=20):
        implementer.prompt(feedback, repo)
        agent_review(repo)
        push(implementer, pr)
```

Whether the reviewer agent runs again after human feedback is the
`agent_review` line in the last loop.

## Design

### 1. Workflow

```python
def workflow(*, trigger: TriggerSpec) -> Callable[[WorkflowFn], Workflow]: ...


class Run(Protocol):
    id: UUID
    work: WorkItem
```

- The workflow's name is the function's name.
- Returning ends the run as `succeeded`. An uncaught exception ends it as
  `failed`.
- The body runs in the control plane and only calls steps and decides from
  their results. Section 7 gives the two rules that follow from how it is
  executed.

### 2. Steps

A step is a function that runs in its own container:

```python
@step
def docs_only(ctx: StepContext, repo: Repo) -> bool:
    ...
```

- Called from a workflow, `docs_only(repo)` looks like an ordinary call.
  Fledge runs the function in a container with the same arguments and the
  call returns its result.
- Arguments and results must be serialisable: built-in types, dataclasses
  and Pydantic models.
- `@step(sandbox=dev)` runs the step with a sandbox.

```python
@dataclass(frozen=True)
class Repo:
    url: str
    path: Path  # a directory inside the run's workspace
```

`clone_repo(url) -> Repo` is built in. Each clone is its own directory, so
one run can hold several repositories.

### 3. Agent

```python
@dataclass(frozen=True)
class Agent:
    name: str
    harness: Harness
    sandbox: Sandbox = Sandbox()

    @overload
    def prompt(self, prompt: str | Feedback, repo: Repo) -> str: ...
    @overload
    def prompt[T](self, prompt: str | Feedback, repo: Repo, *, returns: type[T]) -> T: ...

    def approves(self, prompt: str, repo: Repo) -> Check: ...
```

There is one way to give an agent work: `prompt`.

- It runs the agent in `repo` until it yields and returns its final message.
- The prompt is a string, or the `Feedback` from a gate, which Fledge renders
  as "address these findings".
- With `returns=`, the agent must finish with a value of that type (a
  dataclass or Pydantic model), and the call returns it. This is how a
  workflow gets a decision out of an agent without parsing prose.
- The first prompt an agent receives in a run starts its session. Every
  later one resumes it. A session that cannot be resumed fails the call.

`approves` is the one piece of sugar: a check (section 5) that prompts the
agent with `returns=Review` and maps `approved` to passed and the comments to
feedback.

**Harness.** A harness is a value, `ClaudeCode(model=...)`, so it can carry
options. A custom harness is a class in the workflows directory. What Fledge
asks of a harness is one operation: run a turn given a prompt, a working
directory, extra environment variables, a directory for session state, and
optionally an output schema. Everything in this proposal, including pull
request handling, is built from that.

### 4. Sandbox and Service

```python
@dataclass(frozen=True)
class Service:
    name: str
    image: str
    env: Mapping[str, str] = field(default_factory=dict)
    ready_port: int | None = None


@dataclass(frozen=True)
class Sandbox:
    services: Sequence[Service] = ()
    env: Mapping[str, str] = field(default_factory=dict)
```

- A sandbox applies to every container started for its agent, and to any
  step or check declared with it.
- A service starts with the container and is removed with it. Nothing in a
  service survives from one call to the next.
- A service is reachable on `localhost` and by its `name`.
- `ready_port`: the call does not start until a TCP connection to that port
  succeeds, and fails if it does not within a fixed time.

### 5. Checks and `until`

```python
def until(check: Check, *, max_rounds: int) -> Iterator[Feedback]: ...
```

`until` is the gate. It evaluates the check and then:

- **Passed:** the loop ends.
- **Feedback:** it yields the feedback, the loop body runs, and the check is
  evaluated again. After `max_rounds` rounds it raises `RoundsExhausted`,
  which fails the run unless the workflow catches it.
- **Stopped:** it raises `RunStopped`, which ends the run as `cancelled`.

A check is a value describing something to evaluate. There are two kinds:

```python
@check
def tests_pass(ctx: StepContext, repo: Repo) -> Passed | Feedback:
    ...


@wait
def release_approved(ticket: str, since: datetime) -> Verdict:
    ...
```

- `@check` runs in a container, like a step.
- `@wait` is polled by the control plane with no container. It may also
  return `Pending` or `Stopped`. `since` is when this loop last received
  feedback, or when it started.

```python
@dataclass(frozen=True)
class FeedbackItem:
    author: str
    body: str


@dataclass(frozen=True)
class Feedback:
    items: Sequence[FeedbackItem]


Verdict = Passed | Feedback | Pending | Stopped
```

Built-in checks:

- `agent.approves(prompt, repo)`: see section 3.
- `command(argv, repo, sandbox=...)`: passed on exit status 0, otherwise
  feedback with the tail of the output.
- `checks_green(pr)`: waits for CI. Feedback names the failed checks with
  their log tails.
- `merged(pr)`: waits for the pull request. Passed when merged, stopped when
  closed unmerged, feedback for new reviews, comments and failing checks.

Guidance typed into the web page is feedback too: it is delivered to the
running agent turn, or as the next round of the gate the run is waiting in.

### 6. Pull requests

The agent does the git work, because it needs judgement: writing the
description, and merging in commits a person pushed to the branch. The
workflow still names the moments it happens, so that the reviewer gate can
sit before the pull request exists.

```python
def open_pull_request(agent: Agent, repo: Repo, *, draft: bool = False) -> PullRequest: ...
def push(agent: Agent, pr: PullRequest) -> None: ...
def mark_ready(pr: PullRequest) -> None: ...
```

- Fledge is configured with one GitHub token. Every agent turn has it in its
  environment, so an agent can fetch, push and use `gh` at any time.
- `open_pull_request` and `push` are agent prompts with a check afterwards.
  When the turn ends Fledge verifies the result against GitHub: the branch
  head on the remote equals the local head, and a pull request exists for
  the branch. If not, the call fails.
- Fledge names the branch, so it finds the pull request itself and does not
  rely on the agent's report.
- The built-in prompt for `push` tells the agent to fetch the branch and
  merge in anything new before pushing.
- `mark_ready`, `checks_green` and `merged` need no judgement, so Fledge
  calls GitHub directly with the same token.

Nothing here hooks into a harness, and nothing stops an agent pushing
earlier than the workflow asks. The workflow's calls are where publishing is
requested and verified, not where it is permitted. What limits an agent is
the token's scope and the repository's branch protection.

### 7. How a workflow executes

Fledge records the result of every step, check and wait. To advance a run it
calls the workflow function from the top; calls that already have a result
return it at once, and the first call without one starts its work and
suspends the function by raising through it. Two rules for authors follow:

- **The body must be deterministic.** No I/O, clock or randomness outside
  steps, because the body is run many times and must decide the same way
  each time.
- **Do not catch `BaseException` or use a bare `except:`.** That would
  swallow the suspension. `except Exception` is fine.

**Sync now, possibly async.** Workflows are plain functions because this
model does not need coroutines and the code reads better without `await` on
every line. Reasons we might move to `async def` workflows: an engine that
holds a thread per in-flight synchronous workflow, the cost of re-running
long workflows from the top, `finally` and `with` exits running on every
suspension, and parallel work, which composes more naturally from
coroutines. Switching is mechanical but touches every workflow, so this
should be decided before the API ships.

### 8. Parallel work

We expect to need it: several agents at once, and one run opening pull
requests in several repositories. Workflows as functions that pass values
were chosen partly so this can be added as a helper and not as a new kind of
workflow. Its API is not part of this proposal, and it is the main input to
the sync or async decision.

### 9. What goes away

- `Workflow(name=, trigger=, steps=)`, the `Step` class protocol, `AgentStep`
  and `ticket_to_pr.STEPS`.
- `StepContext.outputs`, step outputs keyed by step name, and prompt
  template fields.
- The `fledge.harnesses` entry point group.
- The `open_pull_request` step that pushes with Fledge's own token.

## Open questions

1. **`repo` on every agent call.** It is explicit and lets one agent work in
   several repositories. The alternative is binding once,
   `dev = implementer.on(repo)`, then `dev.prompt(...)`.
2. **Push as its own call.** The alternative is that once a pull request is
   open, every prompt in that repository may push and is verified. Shorter
   loops, less visible.
3. **Structured results.** `returns=` assumes each harness can be held to an
   output schema. To confirm for both CLIs; the fallback is parsing a
   final-line verdict.
4. **Sync or async workflows** (section 7).
