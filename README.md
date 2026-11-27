# fledge

An open source, modular software factory. It picks up engineering work such as
tickets, runs coding agents on it in containers, and takes the result through
a pull request to merge, with people able to watch and steer.

Status: early MVP. It runs locally on Docker. The design, including what is
deliberately left out, is in [doc/architecture.md](doc/architecture.md).

## How it works

- A **workflow** is a Python function you write, with a **trigger** that polls
  for work. Fledge loads every workflow in your `workflows/` directory. The
  built-in trigger watches GitHub issues that carry a label.
- Each ticket becomes a **run** of its workflow. The function calls **steps**,
  which run in their own containers, and **agents**, which are a coding agent
  CLI (Claude Code or Codex, through a **harness**) with a session that lasts
  the whole run.
- `until(check)` loops until a check passes: an agent approving the change,
  CI going green, or the pull request being merged. Each round hands the
  check's feedback to an agent.
- Agents do the git work: they commit, push and open the pull request when the
  workflow asks, and Fledge verifies against GitHub that they did.
- A small web view shows runs and their progress, and lets you launch a run,
  send guidance to the agents, and cancel.

## Running it locally

You need:

- Docker with Compose.
- A GitHub token for the target repository that can read issues, push
  branches, open and update pull requests, and read checks. For a
  fine-grained token, grant Contents read/write, Issues read, Pull requests
  read/write, Checks read, Commit statuses read and Actions read (for the log
  tails of failed GitHub Actions jobs). At startup fledge checks that the
  token can see the repository, read its issues and push to it, and exits with
  a message if it can't; it can't check the rest without acting on them.
- Credentials for each agent CLI your workflows use, as environment variables
  (step 2).

1. Write a workflow in `workflows/` (any `.py` file; the directory is
   git-ignored). This command writes one that has a Claude Code agent
   implement each issue labelled `fledge`, open a draft pull request, fix CI
   failures, and answer review comments until the pull request is merged:

   ```bash
   uv run fledge init --repo owner/name
   ```

   `--label` (default `fledge`) and `--name` (default `tickets`) change the
   label and workflow name. The file it writes is:

   ```python
   # workflows/tickets.py
   """The workflow runs once per labelled issue. An agent implements the issue,
   opens a pull request, and handles CI failures and review comments until it is
   merged.

   Each step runs in its own container. To resume a run, Fledge calls this
   function again from the top and reuses the results of steps that already ran,
   so keep I/O and clock reads inside steps. Do not catch BaseException: Fledge
   pauses a run by raising one.
   """

   from fledge import Agent, Run, until, workflow
   from fledge.git import clone_repo
   from fledge.github import (
       GitHubIssues,
       checks_green,
       mark_ready,
       merged,
       open_pull_request,
       push,
   )
   from fledge.harnesses import ClaudeCode

   IMPLEMENT = """\
   Resolve this ticket by changing the repository in the current working directory.

   Title: {work.title}
   Link: {work.url}

   {work.body}

   Make the change, run the project's checks if it has any, and commit your work
   on the current branch. Do not push and do not open a pull request yet: you
   will be asked to. End with a short summary of what you changed and why.
   """

   implementer = Agent("tickets_implementer", ClaudeCode())


   @workflow(trigger=GitHubIssues(repo="owner/name", label="fledge"))
   def tickets(run: Run) -> None:
       repo = clone_repo(run.work.repo_url)
       implementer.prompt(IMPLEMENT.format(work=run.work), repo)

       # If CI is still failing after 5 fixes, the run fails.
       pr = open_pull_request(implementer, repo, draft=True)
       for feedback in until(checks_green(pr), max_rounds=5):
           implementer.prompt(feedback, repo)
           push(implementer, pr)
       mark_ready(pr)

       # If the pull request is closed without merging, the run is cancelled.
       for feedback in until(merged(pr), max_rounds=20):
           implementer.prompt(feedback, repo)
           push(implementer, pr)
   ```

   Every module-level workflow in the directory is loaded. Modules in it can
   share code with relative imports (`from .prompts import IMPLEMENT`).

2. Create `.env` with the secrets. Of the names listed in
   `FLEDGE_STEP_SECRETS` (default
   `GITHUB_TOKEN,ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN`), the ones that are
   set are passed to every step container.

   ```
   GITHUB_TOKEN=...
   ANTHROPIC_API_KEY=...
   ```

   To bill a Claude Pro or Max subscription instead of the API, run
   `claude setup-token` on your machine and use the token it prints in place
   of `ANTHROPIC_API_KEY`:

   ```
   GITHUB_TOKEN=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   ```

   Don't set both: Claude Code prefers the API key when it is set. Runs share
   your subscription's usage limits with your own Claude use.

   For a Codex agent, add `CODEX_API_KEY` to `.env` and to the list:

   ```
   CODEX_API_KEY=...
   FLEDGE_STEP_SECRETS=GITHUB_TOKEN,ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN,CODEX_API_KEY
   ```

   Agent credentials must be environment variables. Each agent keeps its
   session state in the run's workspace, so a login stored in `~/.claude` or
   `~/.codex` on your machine or in the image is not seen.

3. Start Postgres and the factory:

   ```bash
   docker compose up --build
   ```

4. Open http://localhost:8000.

The control plane starts step containers through the Docker socket. If your
socket is not at `/var/run/docker.sock`, set `DOCKER_SOCKET` to its path before
running Compose. On Podman, the socket path is the one inside the Podman
machine; a rootful machine already has `/var/run/docker.sock`.

## Using it

**Choose the tickets.** Either add the configured label to issues, and the
trigger picks them up within `FLEDGE_POLL_INTERVAL_SECONDS`, or name them yourself:

```bash
uv run fledge submit --workflow tickets 12 14 15
```

`--workflow` is the workflow's name (the function's name), and each argument
is an issue number. The launch box on the web view's front page does the same
thing. A ticket found by polling is only ever run once; launching by hand
always starts a new run.

**Watch.** Any number of runs can be in progress. At most
`FLEDGE_MAX_CONCURRENT_STEPS` step containers run at once; a run waiting on CI
or on a reviewer holds none. The run page lists every call the workflow has
made, step or wait, with its status and result (the pull request is one of
them), and a live event log.

**Steer.** Type into the guidance box on the run page, or comment on the
issue. Any comment on the issue from a non-bot account counts as guidance. An
agent receives it when its current turn ends, not mid-turn; a run that is
waiting on CI or on review receives it as the next round of feedback. Review
comments on the pull request are feedback for the `merged` gate. Comments by
bots and by the account `GITHUB_TOKEN` belongs to are ignored there, so that
the agent does not answer itself: if the token is your own, use the issue or
the guidance box rather than pull request comments.

**Finish.** With the workflow above, a run succeeds when its pull request is
merged, is cancelled when the pull request is closed without merging, and
fails if a gate runs out of rounds or a step fails.

**Cancel.** The cancel button on the run page stops the running step and ends
the run. There is no retry; launch the ticket again by hand.

## Writing workflows

A fuller workflow: the implementer runs next to a Postgres service, and a
reviewer agent on Codex must approve the change before the pull request is
opened and again after each round of human review.

```python
# workflows/reviewed.py
from fledge import Agent, Repo, Run, Sandbox, Service, until, workflow
from fledge.git import clone_repo
from fledge.github import (
    GitHubIssues,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    push,
)
from fledge.harnesses import ClaudeCode, Codex

IMPLEMENT = """\
Resolve this ticket: {work.title}
{work.url}

{work.body}

Commit your work on the current branch. Do not push.
"""

REVIEW = """\
Review the uncommitted and committed changes on the current branch against
its base. Approve only if they resolve the ticket and the tests pass.
"""

dev = Sandbox(
    services=[
        Service(
            name="postgres",
            image="postgres:17",
            env={"POSTGRES_PASSWORD": "fledge"},
            ready_port=5432,
        )
    ],
    env={"DATABASE_URL": "postgresql://postgres:fledge@postgres/postgres"},
)

implementer = Agent("implementer", ClaudeCode(), sandbox=dev)
reviewer = Agent("reviewer", Codex(), sandbox=dev)


def agent_review(repo: Repo) -> None:
    for feedback in until(reviewer.approves(REVIEW, repo), max_rounds=3):
        implementer.prompt(feedback, repo)


@workflow(trigger=GitHubIssues(repo="owner/name", label="fledge"))
def reviewed(run: Run) -> None:
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

The service starts with each container the agent runs in and is removed with
it; the agent reaches it as `postgres`, and the call waits until port 5432
accepts connections. All agents and steps of a run share one workspace, so the
reviewer sees the implementer's working tree.

`agent.prompt(..., returns=SomeType)` makes the agent finish with a value of
that type (a dataclass or Pydantic model) and returns it; `approves` is built
on it. `command(argv, repo)` in `fledge.checks` is a check that passes when a
command exits with status 0 and otherwise hands its output's tail back as
feedback.

Fledge runs a workflow by calling the function again from the top each time
the run can advance, returning the recorded result of every call already
made. That gives the rules for workflow code:

- **The body must be deterministic.** No I/O, clock or randomness outside
  steps and checks: the body runs many times and must make the same calls in
  the same order each time.
- **Do not catch `BaseException` or use a bare `except:`.** That would
  swallow the suspension Fledge raises through the function. `except
  Exception` is fine, for example around a step call to handle `StepFailed`.
- **Agents, steps, checks and `returns=` types are defined at module level**,
  because a step container imports them from the run's snapshot of the
  workflows directory. Agent names are unique across the directory.
- **Step arguments and results are JSON-serialisable and annotated**:
  built-in types, dataclasses and Pydantic models.

Nothing stops an agent from pushing earlier than the workflow asks: every
agent turn has `GITHUB_TOKEN`, and `git` and `gh` are set up with it. What
limits an agent is the token's scope and the repository's branch protection.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
uv sync
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

Run every check:

```bash
uv run pre-commit run --all-files && uv run pytest
```

The tests need two things from your machine:

- **Postgres.** Each test that uses the database creates and drops its own
  throwaway database on the server at `FLEDGE_TEST_DATABASE_URL` (default
  `postgresql:///postgres`, a local server over its Unix socket).
- **A Docker daemon**, for the runner tests. They use `DOCKER_HOST` and are
  skipped if no daemon is reachable.

Coding rules for contributors and coding agents are in [AGENTS.md](AGENTS.md).

### Running the factory from your checkout

To iterate without rebuilding the image, run the control plane on your machine
and let step containers use your checkout's code. Steps still run in
containers, so agents stay sandboxed.

1. Build the image once, so step containers have the CLIs and dependencies.
   Rebuild only when `pyproject.toml`, `uv.lock` or the `Dockerfile` change.

   ```bash
   docker build -t fledge:dev .
   ```

2. In `.env`, point step containers back at your machine and mount your
   source over the image's:

   ```
   FLEDGE_API_URL=http://host.docker.internal:8000   # host.containers.internal on Podman
   FLEDGE_DOCKER_NETWORK=bridge                      # podman on Podman
   FLEDGE_STEP_SOURCE_DIR=/absolute/path/to/checkout/src
   ```

3. Start the control plane against a local Postgres database:

   ```bash
   createdb fledge
   DATABASE_URL=postgresql:///fledge uv run --env-file .env fledge serve --host 127.0.0.1
   ```

   Set `DOCKER_HOST` if your daemon's socket is not the default (on Podman,
   `unix://$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')`).

After a code change, restart `fledge serve`. The next step container picks up
the change by itself.

### Layout

| Path | What it holds |
| --- | --- |
| `src/fledge/__init__.py` | The workflow author's API |
| `src/fledge/core.py` | The types workflow code and steps use |
| `src/fledge/triggers.py`, `harnesses/base.py` | The interfaces triggers and harnesses implement |
| `src/fledge/engine.py` | Replaying workflow functions; `@step` and `@workflow` |
| `src/fledge/checks.py`, `agents.py` | Checks, waits, `until` and the built-in `command` check; agents |
| `src/fledge/git.py`, `github/`, `harnesses/` | Running git and `clone_repo`, the GitHub trigger and pull requests, Claude Code and Codex |
| `src/fledge/workflows.py` | Loading and snapshotting the workflows directory |
| `src/fledge/scaffold.py` | The workflow `fledge init` writes |
| `src/fledge/executor.py`, `wire.py` | What runs inside a step container, and its contract with the control plane |
| `src/fledge/control/` | The control plane; nothing outside it but the CLI imports it |
| `src/fledge/control/poller.py`, `orchestrator.py` | The two control-plane loops |
| `src/fledge/control/runner/` | Starting step containers and their services; Docker only for now |
| `src/fledge/control/web/` | Public API, internal API for step containers, web view |
| `src/fledge/control/store.py`, `migrations/` | Postgres queries and schema |
| `src/fledge/cli.py` | `fledge serve`, `init`, `submit`, `migrate`, `step-exec` |

### Writing an extension

Steps (`@step`), checks that run in a container (`@check`), checks the control
plane polls (`@wait`), harnesses (a class with a `run_turn` method) and
triggers (a `TriggerSpec` whose `start()` returns a `Trigger`) are plain
Python in the workflows directory, next to the workflows that use them. The
interfaces are in `fledge.triggers`, `fledge.harnesses` and section 3 of the
architecture doc. The control plane snapshots the whole directory when it
starts and hands each run's step containers the snapshot the run started
with, so editing a workflow needs a restart of `fledge serve`, not an image
rebuild.

A third-party library a workflow imports, the CLI a custom harness wraps, or
a toolchain the target repositories need has to be installed in the image:
build one `FROM` the fledge image and set `FLEDGE_IMAGE` to it.
