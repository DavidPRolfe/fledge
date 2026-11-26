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
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path

from fledge.core import Trigger, Workflow
from fledge.store import WorkflowRef

_GITHUB_REPO = re.compile(r"[\w.-]+/[\w.-]+")

_GITHUB_WORKFLOW = """\
from fledge.builtin.triggers.github_issues import GitHubIssues
from fledge.builtin.workflows.ticket_to_pr import STEPS
from fledge.core import Workflow

{name} = Workflow(
    name={name!r},
    trigger=GitHubIssues(repo={repo!r}, label={label!r}),
    steps=STEPS,
)
"""


@dataclass(frozen=True)
class Snapshot:
    hash: str
    files: Mapping[str, str]
    """Source text by path relative to the workflows directory."""


@dataclass(frozen=True)
class TriggerBinding:
    trigger: Trigger
    workflow: WorkflowRef


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
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return Snapshot(hash=digest, files=files)


def load_workflows(files: Mapping[str, str]) -> dict[str, Workflow]:
    """Import the snapshot's top-level modules and collect their Workflow values."""
    # Written to a fresh directory rather than imported from where they were
    # read, so the code that runs is exactly the snapshot. The directory is
    # kept: a step may import from the package lazily.
    directory = Path(tempfile.mkdtemp(prefix="fledge-workflows-"))
    for name, source in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    # A unique package name per load keeps two snapshots in one process apart.
    package = f"fledge_workflows_{uuid.uuid4().hex}"
    spec = ModuleSpec(package, None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    sys.modules[package] = importlib.util.module_from_spec(spec)
    found: dict[str, Workflow] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = importlib.import_module(f"{package}.{path.stem}")
        for value in vars(module).values():
            if not isinstance(value, Workflow):
                continue
            existing = found.setdefault(value.name, value)
            if existing is not value:
                raise ValueError(f"two workflows are named {value.name!r}")
    if not found:
        raise ValueError("the workflow files define no module-level Workflow")
    return found


def start_triggers(
    snapshot_hash: str, workflows: Mapping[str, Workflow]
) -> dict[str, TriggerBinding]:
    """Start each workflow's trigger, keyed by workflow name."""
    return {
        name: TriggerBinding(
            workflow.trigger.start(),
            WorkflowRef(
                name=name,
                steps=tuple(step.name for step in workflow.steps),
                snapshot_hash=snapshot_hash,
            ),
        )
        for name, workflow in workflows.items()
    }


def write_github_workflow(directory: Path, name: str, repo: str, label: str) -> Path:
    """Write the built-in ticket_to_pr steps fed by labelled issues in `repo`.

    Refuses to overwrite an existing file.
    """
    if not name.isidentifier():
        raise ValueError(f"name must be a Python identifier, got {name!r}")
    if not _GITHUB_REPO.fullmatch(repo):
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    # GitHub takes the labels filter as a comma-separated list.
    if not label or "," in label:
        raise ValueError(f"label must be non-empty without commas, got {label!r}")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    with path.open("x") as file:
        file.write(_GITHUB_WORKFLOW.format(name=name, repo=repo, label=label))
    return path
