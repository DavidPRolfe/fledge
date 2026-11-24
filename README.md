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

1. Create the config and point the trigger at your repository and label:

   ```bash
   cp fledge.example.toml fledge.toml
   ```

2. Put the secrets in `.env`. These are the names listed under `step_secrets`
   in `fledge.toml`; they are passed to every step container.

   ```
   GITHUB_TOKEN=...
   ANTHROPIC_API_KEY=...
   ```

   To bill a Claude Pro or Max subscription instead of the API, run
   `claude setup-token` on your machine and use the token it prints:

   ```
   GITHUB_TOKEN=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   ```

   Then replace `ANTHROPIC_API_KEY` with `CLAUDE_CODE_OAUTH_TOKEN` in
   `step_secrets`. Don't pass both: Claude Code prefers the API key when it is
   set. Runs share your subscription's usage limits with your own Claude use.

3. Start Postgres and the factory:

   ```bash
   docker compose up --build
   ```

4. Open http://localhost:8000.

The control plane starts step containers through the Docker socket. If your
socket is not at `/var/run/docker.sock`, set `DOCKER_SOCKET` to its path before
running Compose. On Podman, the socket path is the one inside the Podman
machine, and the `fledge` service also needs `security_opt: [label=disable]`.

## Using it

**Choose the tickets.** Either add the configured label to issues, and the
trigger picks them up within `poll_interval_seconds`, or name them yourself:

```bash
uv run fledge submit --trigger issues 12 14 15
```

`--trigger` is the trigger's `name` in `fledge.toml`, and each argument is an
issue number. The launch box on the web view's front page does the same thing.
A ticket found by polling is only ever run once; launching by hand always
starts a new run.

**Watch.** Runs are worked through `max_concurrent_runs` at a time. The run
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
it. Set `image` in `fledge.toml` to that image and refer to your trigger type
and workflow by their entry point names.
