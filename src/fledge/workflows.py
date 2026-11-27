"""Finds the operator's workflows and snapshots their source for step containers.

The workflows directory is imported as a package, so its modules import each
other with relative imports (`from .prompts import IMPLEMENT`).
"""

import hashlib
import importlib
import importlib.util
import json
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path

from fledge.agents import Agent
from fledge.core import Trigger, Workflow
from fledge.store import Store, WorkflowRef

_GITHUB_REPO = re.compile(r"[\w.-]+/[\w.-]+")

# Braces the written file's own IMPLEMENT template needs are doubled here.
_GITHUB_WORKFLOW = '''\
from fledge import Agent, Run, until, workflow
from fledge.github import (
    GitHubIssues,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    push,
)
from fledge.harnesses import ClaudeCode
from fledge.steps import clone_repo

IMPLEMENT = """\\
Resolve this ticket by changing the repository in the current working directory.

Title: {{work.title}}
Link: {{work.url}}

{{work.body}}

Make the change, run the project's checks if it has any, and commit your work
on the current branch. Do not push and do not open a pull request yet: you
will be asked to. End with a short summary of what you changed and why.
"""

implementer = Agent({agent}, ClaudeCode())


@workflow(trigger=GitHubIssues(repo={repo}, label={label}))
def {name}(run: Run) -> None:
    repo = clone_repo(run.work.repo_url)
    implementer.prompt(IMPLEMENT.format(work=run.work), repo)

    pr = open_pull_request(implementer, repo, draft=True)
    for feedback in until(checks_green(pr), max_rounds=5):
        implementer.prompt(feedback, repo)
        push(implementer, pr)
    mark_ready(pr)

    for feedback in until(merged(pr), max_rounds=20):
        implementer.prompt(feedback, repo)
        push(implementer, pr)
'''


@dataclass(frozen=True)
class Snapshot:
    hash: str
    files: Mapping[str, str]
    """Source text by path relative to the workflows directory."""


@dataclass(frozen=True)
class Loaded:
    workflows: Mapping[str, Workflow]
    agents: Mapping[str, Agent]


@dataclass(frozen=True)
class TriggerBinding:
    trigger: Trigger
    workflow: WorkflowRef


def snapshot_hash(files: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def read_snapshot(directory: Path) -> Snapshot:
    """Read every file under `directory`, skipping dotfiles and __pycache__."""
    if not directory.is_dir():
        raise FileNotFoundError(f"workflows directory {directory} does not exist")
    files: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path.is_file():
            files["/".join(parts)] = path.read_text()
    if not any(name.endswith(".py") for name in files):
        raise ValueError(f"no Python files in workflows directory {directory}")
    return Snapshot(hash=snapshot_hash(files), files=files)


# Importing a snapshot twice would create a second copy of every class and
# function in it, so each snapshot is imported once per process.
_loaded: dict[str, Loaded] = {}


def load_workflows(files: Mapping[str, str]) -> Loaded:
    """Import the snapshot's top-level modules and collect their workflows and agents."""
    digest = snapshot_hash(files)
    cached = _loaded.get(digest)
    if cached is not None:
        return cached
    # Written to a fresh directory rather than imported from where they were
    # read, so the code that runs is exactly the snapshot. The directory is
    # kept: a step may import from the package lazily.
    directory = Path(tempfile.mkdtemp(prefix="fledge-workflows-"))
    for name, source in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    # The package is named after the snapshot's hash: that keeps two snapshots
    # in one process apart, and gives a step function the same `module:qualname`
    # reference in the control plane as in the step container that imports it.
    package = f"fledge_workflows_{digest[:16]}"
    spec = ModuleSpec(package, None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    sys.modules[package] = importlib.util.module_from_spec(spec)
    workflows: dict[str, Workflow] = {}
    agents: dict[str, Agent] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = importlib.import_module(f"{package}.{path.stem}")
        for value in vars(module).values():
            if isinstance(value, Workflow):
                _add(workflows, value.name, value, "workflows")
            elif isinstance(value, Agent):
                _add(agents, value.name, value, "agents")
    if not workflows:
        raise ValueError("the workflow files define no module-level Workflow")
    loaded = Loaded(workflows=workflows, agents=agents)
    _loaded[digest] = loaded
    return loaded


def _add[T](found: dict[str, T], name: str, value: T, kind: str) -> None:
    # Identity, not equality: the same object imported into a second module
    # counts once, but two separately defined ones clash even if equal.
    existing = found.setdefault(name, value)
    if existing is not value:
        raise ValueError(f"two {kind} are named {name!r}")


class SnapshotLoader:
    """Loads the workflows of stored snapshots, so a run keeps the code it started with."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def workflow(self, snapshot_hash: str, name: str) -> Workflow:
        files = await self._store.get_snapshot_files(snapshot_hash)
        workflows = load_workflows(files).workflows
        if name not in workflows:
            raise LookupError(
                f"workflow snapshot {snapshot_hash[:12]} has no workflow {name!r}"
            )
        return workflows[name]


def start_triggers(
    snapshot_hash: str, workflows: Mapping[str, Workflow]
) -> dict[str, TriggerBinding]:
    """Start each workflow's trigger, keyed by workflow name."""
    return {
        name: TriggerBinding(
            workflow.trigger.start(),
            WorkflowRef(name=name, snapshot_hash=snapshot_hash),
        )
        for name, workflow in workflows.items()
    }


def _python_str(value: str) -> str:
    # A JSON string without ASCII escaping is also a valid Python literal for
    # the same text, and uses the double quotes the formatter would.
    return json.dumps(value, ensure_ascii=False)


def write_github_workflow(directory: Path, name: str, repo: str, label: str) -> Path:
    """Write a workflow that turns labelled issues in `repo` into pull requests.

    Refuses to overwrite an existing file.
    """
    if not name.isidentifier():
        raise ValueError(f"name must be a Python identifier, got {name!r}")
    if not _GITHUB_REPO.fullmatch(repo):
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    # GitHub takes the labels filter as a comma-separated list.
    if not label or "," in label:
        raise ValueError(f"label must be non-empty without commas, got {label!r}")
    source = _GITHUB_WORKFLOW.format(
        name=name,
        # Agent names are unique across the workflows directory, and init can
        # write several workflows into one.
        agent=_python_str(f"{name}_implementer"),
        repo=_python_str(repo),
        label=_python_str(label),
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    with path.open("x") as file:
        file.write(source)
    return path
