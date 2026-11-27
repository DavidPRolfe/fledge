"""Ties stored workflow snapshots to the runs and triggers that use them."""

from collections.abc import Mapping
from dataclasses import dataclass

from fledge.control.store import Store, WorkflowRef
from fledge.engine import Workflow
from fledge.triggers import Trigger
from fledge.workflows import load_workflows


@dataclass(frozen=True)
class TriggerBinding:
    trigger: Trigger
    workflow: WorkflowRef


class SnapshotLoader:
    """Loads the workflows of stored snapshots, so a run keeps the code it started with."""

    def __init__(self, store: Store) -> None:
        self._store = store
        # A snapshot never changes under its hash, and the orchestrator asks
        # for the workflow of every running run on every tick.
        self._workflows: dict[str, Mapping[str, Workflow]] = {}

    async def workflow(self, snapshot_hash: str, name: str) -> Workflow:
        workflows = self._workflows.get(snapshot_hash)
        if workflows is None:
            files = await self._store.get_snapshot_files(snapshot_hash)
            workflows = self._workflows[snapshot_hash] = load_workflows(files)
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
