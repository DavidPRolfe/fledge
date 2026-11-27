"""Finds the operator's workflows and snapshots their source for step containers.

The workflows directory is imported as a package, so its modules import each
other with relative imports (`from .prompts import IMPLEMENT`).
"""

import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path

from fledge.agents import Agent
from fledge.engine import Workflow


@dataclass(frozen=True)
class Snapshot:
    hash: str
    files: Mapping[str, str]
    """Source text by path relative to the workflows directory."""


@dataclass(frozen=True)
class Loaded:
    workflows: Mapping[str, Workflow]
    agents: Mapping[str, Agent]


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
