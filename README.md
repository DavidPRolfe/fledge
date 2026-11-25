# fledge

An open source, modular software factory. It picks up engineering work such as
tickets, runs coding agents on it in containers, and produces pull requests,
with people able to watch and steer.

Status: early MVP. It runs locally on Docker. The design, including what is
deliberately left out, is in [doc/architecture.md](doc/architecture.md).

## How it works

- A **trigger** polls for work. The built-in one watches GitHub issues that
  carry a label.
- Each ticket becomes a **run** of a **workflow**, which is an ordered list of
  **steps**. The built-in `ticket_to_pr` workflow clones the repository, has an
  agent implement the ticket, and opens a pull request.
- Each step runs in its own container. Agent steps use a **harness**; Claude
  Code and Codex are built in.
- A small web view shows runs and their progress, and lets you launch a run,
  send guidance to the agent, and cancel.

Triggers, workflows, steps and harnesses are extension points: plain Python
classes registered through entry points. See section 3 of the architecture doc.

## Running it locally

You need Docker with Compose, a GitHub token that can read issues, push
branches and open pull requests on the target repository, and credentials for
Claude Code: either an Anthropic API key or a Claude subscription token.

1. Create `.env` with the secrets and a trigger pointing at your repository
   and label. Of the names listed in `FLEDGE_STEP_SECRETS` (default
   `GITHUB_TOKEN,ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN`), the ones that are
   set are passed to every step container.

   ```
   GITHUB_TOKEN=...
   ANTHROPIC_API_KEY=...
   FLEDGE_TRIGGERS='[{"name": "issues", "type": "github_issues", "workflow": "ticket_to_pr", "repo": "owner/name", "label": "fledge"}]'
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

2. Start Postgres and the factory:

   ```bash
   docker compose up --build
   ```

3. Open http://localhost:8000.

The control plane starts step containers through the Docker socket. If your
socket is not at `/var/run/docker.sock`, set `DOCKER_SOCKET` to its path before
running Compose. On Podman, the socket path is the one inside the Podman
machine; a rootful machine already has `/var/run/docker.sock`.

## Using it

**Choose the tickets.** Either add the configured label to issues, and the
trigger picks them up within `FLEDGE_POLL_INTERVAL_SECONDS`, or name them yourself:

```bash
uv run fledge submit --trigger issues 12 14 15
```

`--trigger` is the trigger's `name` in `FLEDGE_TRIGGERS`, and each argument is an
issue number. The launch box on the web view's front page does the same thing.
A ticket found by polling is only ever run once; launching by hand always
starts a new run.

**Watch.** Runs are worked through `FLEDGE_MAX_CONCURRENT_RUNS` at a time. The run
page shows each step's status, its outputs (including the pull request link)
and a live event log.

**Steer.** Type into the guidance box on the run page, or comment on the
issue. The agent receives guidance when its current turn ends, not mid-turn.
Any comment on the issue from a non-bot account counts as guidance.

**Cancel.** The cancel button on the run page stops the running step and ends
the run. There is no retry; launch the ticket again by hand.

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
the change by itself. Changes to entry points in `pyproject.toml` still need a
rebuild, because the image's installed metadata lists them.

### Layout

| Path | What it holds |
| --- | --- |
| `src/fledge/core.py` | The types and interfaces extensions implement |
| `src/fledge/builtin/` | Shipped extensions, by type: `triggers/`, `steps/`, `harnesses/`, `workflows/` |
| `src/fledge/poller.py`, `orchestrator.py` | The two control-plane loops |
| `src/fledge/runner/` | Starting step containers; Docker only for now |
| `src/fledge/executor.py` | What runs inside a step container |
| `src/fledge/web/` | Public API, internal API for step containers, web view |
| `src/fledge/store.py`, `migrations/` | Postgres queries and schema |
| `src/fledge/cli.py` | `fledge serve`, `submit`, `migrate`, `step-exec` |

### Writing an extension

Write a package that implements the interfaces in `fledge.core` and registers
them under the `fledge.triggers`, `fledge.workflows` or `fledge.harnesses`
entry point groups, then build an image `FROM` the fledge image that installs
it. Set `FLEDGE_IMAGE` to that image and refer to your trigger type
and workflow by their entry point names.
