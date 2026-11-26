# Proposal: workflows as code

Status: implemented.

## Problem

A workflow today is split across three places:

- its steps are a `Workflow` value in an installed package, found through the
  `fledge.workflows` entry point;
- the trigger that feeds it is JSON in `FLEDGE_TRIGGERS`, naming a trigger type
  by entry point and the workflow by name;
- the code reaches step containers only by being baked into the image.

Adding or changing a workflow means editing a package, rebuilding the image and
keeping a JSON string in sync with both. The trigger, which decides what work a
workflow receives, is the part furthest from the workflow.

## Goal

A workflow is one Python value that says what starts it and what it does.
Fledge finds these values in a directory, and runs them. No entry points, no
JSON, no image rebuild to change a workflow.

## What a user writes

```python
# workflows/api_tickets.py
from fledge.builtin.steps.agent import AgentStep
from fledge.builtin.steps.clone_repo import CloneRepo
from fledge.builtin.steps.open_pull_request import OpenPullRequest
from fledge.builtin.triggers.github_issues import GitHubIssues
from fledge.core import Workflow

IMPLEMENT = """..."""

api_tickets = Workflow(
    name="api_tickets",
    trigger=GitHubIssues(repo="acme/api", label="fledge"),
    steps=[
        CloneRepo(),
        AgentStep(name="implement", harness="claude_code", prompt=IMPLEMENT),
        OpenPullRequest(),
    ],
)
```

Two workflows that differ only in their trigger share a `steps` list; that is
ordinary Python, not a feature.

## Design

### 1. `Workflow` gains a trigger

```python
@dataclass(frozen=True)
class Workflow:
    name: str
    trigger: TriggerSpec
    steps: Sequence[Step]
```

One trigger per workflow. The workflow name becomes the trigger identity that
runs are deduplicated on, so the `runs.trigger` column is dropped and
`(workflow, external_id)` replaces `(trigger, external_id)`.

### 2. Triggers are declared, then started in the control plane only

Workflow modules are imported in step containers too (see 4), so declaring a
trigger must not do I/O or read secrets. `GitHubIssues(repo=..., label=...)`
is a cheap frozen value; its arguments are checked when it is started. The
control plane calls
`spec.start()` once at startup to get the live `Trigger`; that is where
`GITHUB_TOKEN` is read and where a missing token crashes startup, as it does
now.

```python
class TriggerSpec(Protocol):
    def start(self) -> Trigger: ...
```

The existing `GitHubIssuesTrigger` class stays as the live object;
`GitHubIssues` is the small spec in front of it.

### 3. Discovery

New setting `FLEDGE_WORKFLOWS_DIR` (default `workflows`, relative to the
working directory; compose mounts `./workflows` there). At startup the control
plane imports every `*.py` file in it as a module and collects every
module-level `Workflow` value. It fails startup if:

- the directory is missing or contains no workflows (this also catches the
  Docker "missing bind mount became an empty directory" mistake);
- two workflows share a name;
- a module fails to import.

The directory is imported as a package, so its files can import each other
(shared prompts, custom steps). Third-party libraries still have to be in the
image.

### 4. Getting the code into step containers

Step containers run the step's `run()` method, so they need the same code. I
recommend the control plane deliver it, rather than mounting or baking it in:

- At startup, the control plane reads the directory's files, hashes them, and
  stores the snapshot (`workflow_snapshots`: hash, files as `jsonb`
  path-to-source) if that hash is new.
- Each run records the snapshot hash it was created under, and its workflow's
  step names, so the orchestrator never imports workflow code to advance a run.
- `GET /internal/step-runs/{id}` returns the snapshot's files alongside the
  context. The executor writes them to a directory outside the workspace,
  imports them, and runs the step.

Why this over the alternatives:

| | Control plane delivers (recommended) | Bind-mount the directory | Bake into image |
| --- | --- | --- | --- |
| Change a workflow | Restart `fledge serve` | Restart `fledge serve` | Rebuild image |
| Works when the control plane is itself in a container | Yes | No: it only knows its own path, not the host path to mount | Yes |
| Works on Kubernetes later | Yes | No host paths | Yes |
| A run keeps the code it started with | Yes, pinned by hash | No: an edit changes the remaining steps | Until image changes |
| Agent can tamper with later steps' code | No: outside the workspace volume | No | No |

The cost is a migration, one table and a larger context response. Snapshots are
small (source text) and shared across runs.

### 5. What goes away

- `FLEDGE_TRIGGERS`, `TriggerConfig` and its options-collecting validator.
- The `fledge.workflows` and `fledge.triggers` entry point groups and their
  registry lookups. User code imports trigger classes directly.
- The shipped `ticket_to_pr` workflow value: it can't exist without a repo.
  Its prompt and steps stay importable, and the README shows the example above.

Harnesses keep their entry points for now. `AgentStep` could take a harness
object instead of a name, but that is a separate change.

### 6. Web and CLI

The launch box and `fledge submit` pick a workflow instead of a trigger:
`fledge submit --workflow api_tickets 12 14`. The run list shows the workflow.

## Rollout

1. Core: `Workflow.trigger`, `TriggerSpec`, `GitHubIssues` spec, discovery from
   `FLEDGE_WORKFLOWS_DIR`, migration (drop `runs.trigger`, add snapshot table
   and `runs.snapshot_hash`), web and CLI switch to workflow names. Delete
   `FLEDGE_TRIGGERS` and the two entry point groups.
2. Snapshot delivery to step containers.
3. Docs: README quick start, `doc/architecture.md` sections 3 and 4.

Step 1 alone works for the local "run from your checkout" setup if the
workflows directory sits inside `FLEDGE_STEP_SOURCE_DIR`; step 2 is what makes
it work under Compose. They could land as one change or two.

Existing runs in a dev database have no snapshot; the migration can fail them
rather than invent one. There is no production data yet.

## Decisions

1. **One trigger per workflow, required.** Manual launch is not a second
   trigger: it goes through the same trigger's `fetch(ref)`, which turns a
   ticket reference into a `WorkItem`, and the run is recorded with
   `origin = 'manual'` as now. Multiple triggers can be added later by turning
   `trigger` into a mapping.
2. **Snapshot delivery** as in section 4.
3. **Operator workflows only, for now.** This change reads workflows only from
   `FLEDGE_WORKFLOWS_DIR`, not from the repositories fledge works on. That is
   where things stand today, not a rule: repository workflows are worth
   revisiting once there is a trust model for them, since their code would
   run in the control plane, which holds the Docker socket and every secret.
4. **Default directory** is `workflows/`.
