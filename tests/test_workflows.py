from pathlib import Path

import pytest

from fledge.builtin.triggers.github_issues import GitHubIssues, MissingTokenError
from fledge.core import Trigger
from fledge.store import WorkflowRef
from fledge.workflows import load_workflows, read_snapshot, start_triggers

TICKETS = """\
from fledge.core import Workflow

from .shared import Noop, StartsFake

tickets = Workflow(name="tickets", trigger=StartsFake(), steps=[Noop("a"), Noop("b")])
"""
SHARED = """\
class FakeTrigger:
    async def poll(self):
        return []

    async def fetch(self, ref):
        raise ValueError(ref)

    async def poll_guidance(self, item_external_id, since):
        return []


class Noop:
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return {}


class StartsFake:
    def start(self):
        return FakeTrigger()
"""


def write(directory: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return directory


def test_snapshot_reads_source_and_skips_caches_and_dotfiles(tmp_path: Path) -> None:
    directory = write(
        tmp_path,
        {
            "tickets.py": TICKETS,
            "prompts/implement.md": "Fix it",
            "__pycache__/tickets.cpython-313.pyc": "",
            ".DS_Store": "",
        },
    )

    snapshot = read_snapshot(directory)

    assert snapshot.files == {"tickets.py": TICKETS, "prompts/implement.md": "Fix it"}
    assert read_snapshot(directory).hash == snapshot.hash
    (directory / "prompts/implement.md").write_text("Fix it properly")
    assert read_snapshot(directory).hash != snapshot.hash


def test_snapshot_rejects_a_missing_or_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        read_snapshot(tmp_path / "missing")
    with pytest.raises(ValueError, match="no Python files"):
        read_snapshot(tmp_path)


def test_workflows_are_found_across_modules_with_relative_imports() -> None:
    workflows = load_workflows({"tickets.py": TICKETS, "shared.py": SHARED})

    assert list(workflows) == ["tickets"]
    assert [step.name for step in workflows["tickets"].steps] == ["a", "b"]


def test_a_workflow_imported_into_another_module_counts_once() -> None:
    reexport = "from .tickets import tickets\n"

    workflows = load_workflows(
        {"tickets.py": TICKETS, "shared.py": SHARED, "zz.py": reexport}
    )

    assert list(workflows) == ["tickets"]


def test_two_snapshots_in_one_process_stay_apart() -> None:
    first = load_workflows({"tickets.py": TICKETS, "shared.py": SHARED})
    renamed = TICKETS.replace('name="tickets"', 'name="renamed"')
    second = load_workflows({"tickets.py": renamed, "shared.py": SHARED})

    assert list(first) == ["tickets"]
    assert list(second) == ["renamed"]


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (
            {"tickets.py": TICKETS, "shared.py": SHARED, "copy.py": TICKETS},
            "two workflows are named 'tickets'",
        ),
        ({"shared.py": SHARED}, "define no module-level Workflow"),
    ],
)
def test_load_rejects_duplicate_or_missing_workflows(
    files: dict[str, str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        load_workflows(files)


def test_start_triggers_binds_each_workflow_to_its_snapshot() -> None:
    workflows = load_workflows({"tickets.py": TICKETS, "shared.py": SHARED})

    [(name, binding)] = start_triggers("abc123", workflows).items()

    assert name == "tickets"
    assert isinstance(binding.trigger, Trigger)
    assert binding.workflow == WorkflowRef("tickets", ("a", "b"), "abc123")


def test_github_issues_reads_the_token_only_when_started() -> None:
    spec = GitHubIssues(repo="acme/widgets", label="fledge")

    with pytest.raises(MissingTokenError):
        spec.start()
