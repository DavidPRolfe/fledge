# Proposal: workflows as functions, agents and sandboxes

Status: proposed. The engine in section 7 is unproven and needs a spike before
this is accepted.

## Problem

The workflow we most want to run is one implementer agent taking a ticket to a
merged pull request, reacting to feedback on the way:

1. An implementer agent works on the ticket in a container that can run the
   project, including the services it needs (for Fledge itself, Postgres).
2. A reviewer agent on a different harness reviews the change. Reviewer and
   implementer go back and forth until the reviewer approves.
3. A draft pull request is opened. The implementer fixes CI failures until the
   checks pass, and the pull request is marked ready for review.
4. The implementer responds to human review until the pull request is merged,
   optionally passing each response through the reviewer agent first.

The current model cannot express this:

- A run ends when the pull request opens, so CI and human review happen after
  the run is over.
- Steps run once each, in order. There is no loop.
- An agent exists only inside one `AgentStep`. Its session is lost when the
  step's container exits, so a later step cannot continue the conversation.
- A step container has no way to get a service such as Postgres next to it.
- A run occupies a container, and a concurrency slot, for as long as it is
  active. Waiting days for a human that way is not workable.

We also want the design to survive the next round of requirements without
another rewrite: agents working in parallel, one run opening pull requests in
several repositories, and logic that skips or adds steps depending on what
happened.

## Goal

The workflow above is a short function that reads top to bottom, and the
harder cases are written with ordinary Python (`if`, `for`, variables,
functions) and not with constructs Fledge has to add one at a time.

## Principles

These replace or extend choices in `doc/architecture.md`.

1. **A run is the life of one change**, from ticket to merged or closed pull
   request. Review findings, CI results and human comments about that change
   all belong to the same run.
2. **A workflow is a plain Python function that really executes.** Loops and
   branches are Python. Fledge records each step's result so the function can
   be resumed after a restart or a long wait. Workflows are written without
   `async`; section 7 says why, and what could force that to change.
3. **Steps hand each other values.** A checkout, a pull request and a round of
   feedback are values passed as arguments, not things a step looks up by
   another step's name.
4. **An agent is a value with identity.** It has a harness, a sandbox and a
   session that lasts the run. Any number of steps can give it a turn.
5. **A sandbox is rebuilt each time it is entered.** An agent's files (the
   workspace) and memory (its session) persist. Its container and services do
   not.
6. **Work happens in containers; waiting and deciding happen in the control
   plane.** A run waiting on CI or a person holds no container.
7. **The run page shows what has executed**, grouped by loop round and
   parallel track, and what the run is waiting on. It does not show steps the
   run has not reached, because they are not known until the code gets there.

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

DEV = Sandbox(
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

implementer = Agent("implementer", ClaudeCode(), sandbox=DEV)
reviewer = Agent("reviewer", Codex())


def agent_review(repo: Repo) -> None:
    for feedback in until(reviewer.approves(REVIEW, repo), max_rounds=3):
        implementer.address(feedback, repo)


@workflow(trigger=GitHubIssues(repo="DavidPRolfe/fledge", label="fledge"))
def fledge(run: Run) -> None:
    repo = clone_repo(run.work.repo_url)
    summary = implementer.turn(IMPLEMENT.format(work=run.work), repo)
    agent_review(repo)

    pr = open_pull_request(repo, run.work, description=summary, draft=True)
    for feedback in until(checks_green(pr), max_rounds=5):
        implementer.address(feedback, repo)
        push(pr)
    mark_ready(pr)

    for feedback in until(merged(pr), max_rounds=20):
        implementer.address(feedback, repo)
        agent_review(repo)
        push(pr)
```

Whether the reviewer agent runs again after human feedback is the
`agent_review` line in the last loop.

The harder case, for comparison. Nothing in it needs a feature beyond
`parallel`:

```python
def track(run: Run, repo_url: str) -> tuple[Agent, PullRequest]:
    agent = IMPLEMENTERS[repo_url]
    repo = clone_repo(repo_url)
    summary = agent.turn(IMPLEMENT.format(work=run.work), repo)
    if not docs_only(repo):
        agent_review(agent, repo)
    return agent, open_pull_request(repo, run.work, description=summary)


@workflow(trigger=...)
def multi_repo(run: Run) -> None:
    tracks = parallel(partial(track, run, url) for url in REPOS)
    parallel(partial(until_green, agent, pr) for agent, pr in tracks)
```

## Design

### 1. Workflow

```python
def workflow(*, trigger: TriggerSpec) -> Callable[[WorkflowFn], Workflow]: ...


class Run(Protocol):
    id: UUID
    work: WorkItem
```

- The workflow's name is the function's name.
- The loader collects every module-level `Workflow` in the workflows
  directory, as today.
- The function body runs in the control plane. It may call steps, checks and
  other plain functions, and decide things from their results. It must not do
  I/O, read the clock or use randomness itself: Fledge runs the body again
  from the top many times and feeds it the recorded results, so anything it
  decides must come out the same. All real work belongs in steps.
- A workflow must not catch `BaseException` or use a bare `except:`. Fledge
  suspends a workflow by raising through it (see 7).
- Returning ends the run as `succeeded`. An uncaught exception ends it as
  `failed`.

### 2. Steps

A step is a function that runs in its own container:

```python
@step
def docs_only(ctx: StepContext, repo: Repo) -> bool:
    ...
```

- Called from a workflow, `docs_only(repo)` looks like an ordinary call.
  Fledge starts a container, runs the function there with the same
  arguments, and the call returns its result.
- The step's own body runs in a container and may be `def` or `async def`.
- Arguments and results cross a process boundary, so they must be values
  Fledge can serialise: built-in types, dataclasses and Pydantic models.
- `@step(sandbox=DEV)` runs the step with a sandbox (see 4).
- `StepContext` keeps `run_id`, `work`, `workspace`, `log` and `agent_event`.
  `outputs`, `claim_guidance` and `harness` go away: earlier results arrive as
  arguments, and agents own the rest.

Built-in steps and the values they pass:

```python
@dataclass(frozen=True)
class Repo:
    url: str
    path: Path  # a directory inside the run's workspace


@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int
    repo: Repo
    branch: str
```

- `clone_repo(url) -> Repo`
- `open_pull_request(repo, work, *, description, draft=False) -> PullRequest`
- `push(pr) -> None`: commits anything uncommitted and pushes the branch.
- `mark_ready(pr) -> None`

Each clone is its own directory in the workspace, so one run can hold several
repositories.

### 3. Agent

```python
@dataclass(frozen=True)
class Agent:
    name: str
    harness: Harness
    sandbox: Sandbox = Sandbox()

    def turn(self, prompt: str, repo: Repo) -> str: ...
    def address(self, feedback: Feedback, repo: Repo) -> str: ...
    def approves(self, prompt: str, repo: Repo) -> Check: ...
```

- `turn` runs the agent in `repo` until it yields and returns its final
  message.
- `address` is a turn whose prompt is the feedback, wrapped in a built-in
  instruction to act on it.
- `approves` is a check (see 5): a turn that must end in a verdict.
- The first call an agent receives in a run starts its session. Every later
  call resumes it. If the session cannot be resumed the step fails; Fledge
  does not start a fresh one in its place.
- Prompts are plain strings. The author formats them, so there are no
  template fields for Fledge to define.

**Harness as a value.** `ClaudeCode(model=...)` replaces `harness="claude_code"`,
so a harness can carry options. Workflow code already reaches step containers
through the snapshot, so a custom harness is a class in the workflows
directory and the `fledge.harnesses` entry point group goes away.

**Sessions.** Both shipped harnesses keep session files in the container's
home directory, which does not survive the step. Each run gets a second
volume, mounted at `/state`, and each agent gets `/state/<agent name>` as its
harness state directory. It is separate from the workspace so session files
never appear in a repository's working tree. `Harness.run_turn` gains a
`state_dir: Path` argument. The session id is stored per (run, agent) in
Postgres.

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
- A service starts with the step container and is removed with it. Nothing in
  a service survives from one step to the next.
- A service shares the step container's network namespace. It is reachable on
  `localhost` and by its `name`, on both platforms.
- `ready_port`: before running the step, `fledge step-exec` waits for a TCP
  connection to that port to succeed, and fails the step if it does not
  within a fixed time.
- `env` is added to the step container's environment. Service `env` goes to
  the service's container only.

Platform mapping. `StepSpec` gains `services` and the runner stays at five
operations:

| | Docker | Kubernetes |
| --- | --- | --- |
| Service container | Container started after the step container with `network_mode="container:<step container>"` | Native sidecar in the step's `Job` pod |
| Reach by name | `extra_hosts` on the step container mapping the name to `127.0.0.1` | `hostAliases` mapping the name to `127.0.0.1` |
| Cleanup | `stop_step` removes every container labelled with the step run id | The pod's end removes the sidecars |
| Step state | The step container only | The `Job` status |

**Compose network split.** Step containers currently join the network the
control plane's own Postgres is on, with the password in `compose.yaml`. An
agent told to use Postgres could reach it. `compose.yaml` moves `postgres` to
a second network that only the `fledge` service joins.

### 5. Checks and `until`

```python
def until(check: Check, *, max_rounds: int) -> Iterator[Feedback]: ...
```

`until` is the gate. It runs the check and then:

- **Passed:** the loop ends.
- **Feedback:** if `max_rounds` rounds have already run, it raises
  `RoundsExhausted`. Otherwise it yields the feedback, the loop body runs,
  and the check runs again.
- **Stopped:** it raises `RunStopped`, which ends the run as `cancelled` (for
  example the pull request was closed without merging).

`max_rounds` is required. An uncaught `RoundsExhausted` fails the run. Because
it is an exception, a workflow that wants a different outcome can catch it.

A check is a value describing something to evaluate, so `until` can evaluate
it once per round. There are two kinds:

```python
@check
def tests_pass(ctx: StepContext, repo: Repo) -> Passed | Feedback:
    ...


@wait
def release_approved(ticket: str, since: datetime) -> Verdict:
    ...
```

- `@check` runs in a container, like a step, and may take a sandbox.
- `@wait` is polled by the control plane on an interval, with no container.
  It may also return `Pending` (ask again later) or `Stopped`. `since` is
  when this loop last received feedback, or when it started.

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

- `agent.approves(prompt, repo)`: Fledge appends an instruction that the
  final message must end with `VERDICT: approve` or `VERDICT: request
  changes`. Approve is `Passed`. Request changes is `Feedback` with the rest
  of the message as its body. Anything else fails the step.
- `command(argv, repo, sandbox=...)`: `Passed` on exit status 0, otherwise
  `Feedback` with the tail of the output. This is how "the tests pass
  locally" becomes a gate.
- `checks_green(pr)`: waits. `Pending` while checks run, `Passed` when all
  succeed, `Feedback` naming the failed checks with their log tails
  otherwise. If the previous round ended without a new commit on the pull
  request, it fails the step saying so, since the same failure would only be
  reported again.
- `merged(pr)`: waits. `Passed` when merged, `Stopped` when closed unmerged,
  `Feedback` for reviews and comments made since the last round and for
  failing checks on the head, `Pending` otherwise.

### 6. `parallel`

```python
def parallel[T](calls: Iterable[Callable[[], T]]) -> list[T]: ...
```

Runs the calls at the same time and returns their results in order. If one
fails, the others are cancelled and the failure propagates. The concurrency
limit counts step containers, not runs, so parallel tracks queue for slots
like anything else.

It takes callables, not results, because a plain call would run to
completion before the next one started. Use `functools.partial`, as in the
example above. A `lambda` in a comprehension is a trap: every lambda sees the
loop variable's last value.

### 7. Engine

The orchestrator stops tracking a position in a list. Instead:

- Every step, check evaluation and wait is a **call** with an id derived
  from its position in the execution (its order within its parallel track).
  Calls are rows in Postgres with their arguments, status and result.
- On each tick, for each active run, the control plane runs the workflow
  function from the top. A call that already has a result returns it at
  once. A call without one is started if it has not been, and then raises
  `Suspend`, which unwinds the function back to the engine. The next tick
  runs it again and gets further.
- `parallel` runs each of its callables in turn and catches `Suspend` from
  each, so every track advances as far as it can. If any track suspended,
  `parallel` then suspends itself. Several calls can be outstanding at once.
- `Suspend` derives from `BaseException`, so `except Exception` in a workflow
  does not catch it. If a workflow swallows it anyway, the engine sees the
  function carry on after a suspension and fails the run saying so.
- The control plane still holds no state in memory. A restart repeats the
  same procedure and arrives at the same place.
- Each run executes its own workflow snapshot, so editing the workflows
  directory does not disturb runs in flight.
- Runs gain the status `waiting`: every outstanding call is a wait. A waiting
  run holds no container.
- The step timeout applies to container calls. Waits have no timeout. Cancel
  works in any state.
- The workspace and state volumes live until the run ends.

`until` and `parallel` record where they start and end, and each round, so
the run page can nest what executed under the loop and track it belongs to.

**Sync now, possibly async later.** Workflows are plain functions because
replay does not need coroutines, and the simple workflow reads better without
`await` on every line. Four things could force a move to `async def`
workflows:

- **The adopted engine.** If we adopt a library and its synchronous
  workflows hold a thread for as long as a run is in flight, hundreds of
  runs waiting on review would hold hundreds of threads. Async would be the
  way out. Not yet checked for DBOS.
- **Replay cost.** Every tick re-runs every active run from the top. That is
  cheap while journals are short. A run with thousands of calls, or
  thousands of active runs, may need suspended workflows kept alive in
  memory between ticks, which needs coroutines.
- **Unwinding surprises.** Because suspension is an exception, `finally`
  blocks and `with` exits in a workflow run on every tick, not once. A
  suspended coroutine does not unwind, so this goes away under async.
- **`parallel`.** With coroutines it would take calls directly,
  `parallel(track(run, url) for url in REPOS)`, with no `partial` and no
  lambda trap.

The spike measures the first two. Changing later is mechanical (add `async`
and `await`) but touches every workflow, so the decision should be final
before stage 1 ships. Step, check and wait bodies are unaffected either way.

**Build or adopt.** This is journaled durable execution, which existing
libraries provide. DBOS is the candidate that matches Fledge's constraints (a
Python library with Postgres as its only backend). It would own the call
journal and the resume logic; Fledge would keep the runner, sandboxes, agents,
checks, triggers and web view. Whether it copes with calls that wait for days
is not known. The spike decides between it and a small engine of our own.

### 8. Feedback and guidance

- The `guidance` table and the web box stay, renamed to feedback, and
  `Comment` becomes `FeedbackItem`.
- When an agent turn is running, guidance is delivered as today: the turn
  claims it when it ends and runs another turn.
- When the run is waiting in an `until`, guidance is yielded as that loop's
  next round of feedback.
- Guidance that arrives at any other moment is delivered at the next of
  those two points.

### 9. What goes away

- `Workflow(name=, trigger=, steps=)` as a value, the `Step` protocol as a
  class with `name` and `run`, and `AgentStep`.
- `StepContext.outputs`, step outputs keyed by step name, and prompt template
  fields.
- `runs.steps` and `step_runs.index`.
- The `fledge.harnesses` entry point group.
- "A run ends when the pull request is opened" and "steps run in order, each
  exactly once".
- `ticket_to_pr.STEPS`, replaced by a function taking the two agents,
  which a workflow calls in one line.

## Rollout

Each stage leaves a workflow that runs end to end.

0. **Spike.** The workflow above on fake steps: a control-plane restart
   mid-run, a wait of arbitrary length, and one `parallel` pair. Run it on
   DBOS and decide build or adopt. Also decide sync or async: check what a
   waiting synchronous workflow costs, and time a tick with many active runs.
1. **Workflows as functions.** `@workflow`, `@step`, the call journal, and
   the three existing steps ported. Behaviour is unchanged: clone, one agent
   turn, open a pull request.
2. **Agents and sandboxes.** `Agent`, harness values, `Sandbox`, `Service`,
   services in the Docker runner, the state volume and session resume, the
   compose network split. The implementer can run Fledge's tests against
   Postgres.
3. **Gates with working checks.** `until`, `@check`, `Agent.approves`,
   `command`, and the nested run page. Delivers the reviewer loop.
4. **Gates that wait.** The `waiting` status, `@wait`, `checks_green`, draft
   pull requests, `push`, `mark_ready`. Delivers the CI loop.
5. **The merge gate.** `merged`, and guidance as feedback. Delivers human
   review.
6. **`parallel`.**

Stage 2 is what the original need (an agent sandbox with Postgres) depends
on. It could go before stage 1 by giving today's `AgentStep` an `agent`
argument, at the cost of an API that stage 1 then replaces.

The Kubernetes runner remains deferred. Section 4 is what it has to
implement.

## Risks

1. **The engine is unproven here.** Neither the replay procedure nor DBOS has
   been tried against this design. Stage 0 exists to find out.
2. **Workflow bodies must be deterministic.** A body that reads the clock or
   calls the network decides differently on replay, and the failure is
   confusing. The rule is simple to state and easy to break by accident.
3. **No picture of what is still to come.** The run page shows the executed
   structure only. The up-front view is the source. Deriving a graph from the
   source is possible later without changing how workflows are written.
4. **Some mistakes surface at run time**, not at load: an unbounded `while`,
   or a check used in a place it cannot work.
5. **Long-lived runs meet upgrades.** A run can span a Fledge upgrade. Its
   snapshot runs against the new code, and fails if an API it uses changed.
6. **Volumes.** Two per open run, held for days. Parallel steps mount the
   same workspace at once, which a `ReadWriteOnce` claim on Kubernetes only
   allows on one node.
7. **Pull request comments become prompts**, extending risk 2 in the
   architecture doc. There is no author filter.
8. **Verdict by convention.** The reviewer's verdict is parsed from its final
   message. A model that ignores the instruction fails the step.
9. **No end-to-end run of Fledge inside the sandbox.** With Postgres the
   implementer can run the tests and `fledge serve`, but not step containers,
   which need a Docker daemon.
10. **Sync workflows may not hold.** Section 7 lists what could force
    `async def` workflows. If that happens after workflows exist, each one
    has to be rewritten.
11. **Suspension is an exception.** A workflow with a bare `except:`
    swallows it, and cleanup code in a workflow runs on every tick. The
    engine detects the first; the second is a rule authors have to know.

## Decisions

Made:

- Workflows are functions that execute, not a declared graph and not a
  recorded script.
- The run page shows what executed, not what might.
- Running out of `max_rounds` fails the run unless the workflow catches it.
  `max_rounds` is required.
- A session that cannot be resumed fails the step.
- Service lifetime is the step container's. Sandboxes are rebuilt on entry.
- One workflow covers the change through merge.

For review:

1. **`repo` on every agent call.** Explicit, and it lets one agent work in
   several repositories. The alternative is binding an agent to a checkout
   once, which is shorter in the single-repository case.
2. **`push(pr)` is explicit.** `checks_green` fails loudly when a round ends
   without a new commit. The alternative is pushing automatically after each
   round once the pull request is open.
3. **Reviewer verdict format.** A final-line convention keeps the harness
   interface small. The alternative is structured output through each CLI's
   schema option.
4. **`for ... in until(...)` as the gate's shape.** The alternative is a
   function taking the check and a callback, which hides the loop.
5. **Build or adopt the engine**, after the spike. Adopting DBOS adds a
   dependency.
6. **Stage order.** Functions first, or agents and sandboxes first.
7. **Sync or async workflows**, after the spike. Sync is proposed. Section 7
   lists what would change that.
