import sys
from pathlib import Path

import pytest

from fledge import Agent, engine
from fledge.control.snapshots import SnapshotLoader, start_triggers
from fledge.control.store import Store, WorkflowRef
from fledge.github import GitHubIssues
from fledge.github.api import MissingTokenError
from fledge.harnesses import ClaudeCode
from fledge.scaffold import write_github_workflow
from fledge.triggers import Trigger
from fledge.workflows import load_workflows, read_snapshot, snapshot_hash

TICKETS = """\
from fledge import Run, workflow

from .shared import StartsFake


@workflow(trigger=StartsFake())
def tickets(run: Run) -> None:
    pass
"""
AGENTS = """\
from fledge import Agent

from .shared import FakeHarness

helper = Agent("helper", FakeHarness())
"""
SHARED = """\
from dataclasses import dataclass


class FakeTrigger:
    def check(self):
        pass

    def poll(self):
        return []

    def fetch(self, ref):
        raise ValueError(ref)

    def poll_guidance(self, item_external_id, since):
        return []


class StartsFake:
    def start(self):
        return FakeTrigger()


@dataclass(frozen=True)
class FakeHarness:
    def run_turn(self, turn, on_event):
        raise AssertionError("not prompted in these tests")
"""
FILES = {"tickets.py": TICKETS, "agents.py": AGENTS, "shared.py": SHARED}


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


def test_workflows_are_found_among_modules_that_define_other_things() -> None:
    assert list(load_workflows(FILES)) == ["tickets"]


def test_a_workflow_imported_into_another_module_counts_once() -> None:
    reexport = "from .tickets import tickets\n"

    assert list(load_workflows(FILES | {"zz.py": reexport})) == ["tickets"]


def test_loading_a_snapshot_again_returns_the_same_objects() -> None:
    files = FILES | {"marker.py": "# loaded twice\n"}

    first = load_workflows(files)
    second = load_workflows(dict(files))

    assert second is first
    tickets = first["tickets"]
    # Step containers find functions by this reference, so it must not depend
    # on the process that loads the snapshot.
    package = f"fledge_workflows_{snapshot_hash(files)[:16]}"
    assert engine.ref(tickets.fn) == f"{package}.tickets:tickets"


def test_two_snapshots_in_one_process_stay_apart() -> None:
    first = load_workflows(FILES)
    renamed = TICKETS.replace("def tickets(", "def renamed(")
    second = load_workflows(FILES | {"tickets.py": renamed})

    assert list(first) == ["tickets"]
    assert list(second) == ["renamed"]
    assert first["tickets"].fn.__module__ != second["renamed"].fn.__module__


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (FILES | {"copy.py": TICKETS}, "two workflows are named 'tickets'"),
        ({"shared.py": SHARED}, "define no module-level Workflow"),
    ],
)
def test_load_rejects_duplicate_names_or_no_workflow(
    files: dict[str, str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        load_workflows(files)


@pytest.mark.anyio
async def test_snapshot_loader_returns_the_stored_snapshots_workflow(
    database_url: str,
) -> None:
    edited = FILES | {"tickets.py": TICKETS.replace("pass", "return None")}
    load_workflows(edited)
    async with Store(database_url) as store:
        await store.save_snapshot(snapshot_hash(FILES), FILES)
        loader = SnapshotLoader(store)

        workflow = await loader.workflow(snapshot_hash(FILES), "tickets")

        assert workflow is load_workflows(FILES)["tickets"]
        assert workflow is not load_workflows(edited)["tickets"]
        with pytest.raises(LookupError, match="no workflow 'missing'"):
            await loader.workflow(snapshot_hash(FILES), "missing")


def test_start_triggers_binds_each_workflow_to_its_snapshot() -> None:
    loaded = load_workflows(FILES)

    [(name, binding)] = start_triggers("abc123", loaded).items()

    assert name == "tickets"
    assert isinstance(binding.trigger, Trigger)
    assert binding.workflow == WorkflowRef("tickets", "abc123")


def test_github_issues_reads_the_token_only_when_started() -> None:
    spec = GitHubIssues(repo="acme/widgets", label="fledge")

    with pytest.raises(MissingTokenError):
        spec.start()


def test_written_github_workflows_load_with_their_triggers_and_agents(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "workflows"
    write_github_workflow(directory, "tickets", "acme/api", 'ai "now"')
    write_github_workflow(directory, "bugs", "acme/web", "bug")

    loaded = load_workflows(read_snapshot(directory).files)

    tickets = loaded["tickets"]
    assert tickets.trigger == GitHubIssues(repo="acme/api", label='ai "now"')
    assert loaded["bugs"].trigger == GitHubIssues(repo="acme/web", label="bug")
    module = vars(sys.modules[tickets.fn.__module__])
    assert module["implementer"] == Agent("implementer", ClaudeCode())
    prompt = module["IMPLEMENT"]
    assert prompt.startswith("Resolve this ticket")
    assert "Title: {work.title}\nLink: {work.url}\n\n{work.body}\n" in prompt


def test_write_github_workflow_does_not_overwrite(tmp_path: Path) -> None:
    write_github_workflow(tmp_path, "tickets", "acme/api", "fledge")

    with pytest.raises(FileExistsError):
        write_github_workflow(tmp_path, "tickets", "acme/other", "fledge")


@pytest.mark.parametrize(
    ("name", "repo", "label", "error"),
    [
        ("my-tickets", "acme/api", "fledge", "name"),
        ("tickets", "acme", "fledge", "repo"),
        ("tickets", "acme/api/extra", "fledge", "repo"),
        ("tickets", "acme/api", "a,b", "label"),
        ("tickets", "acme/api", "", "label"),
    ],
)
def test_write_github_workflow_rejects_bad_arguments(
    tmp_path: Path, name: str, repo: str, label: str, error: str
) -> None:
    with pytest.raises(ValueError, match=f"^{error} "):
        write_github_workflow(tmp_path, name, repo, label)

    assert not any(tmp_path.iterdir())
