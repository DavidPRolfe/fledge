from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from fledge.control.poller import Poller
from fledge.control.settings import Settings
from fledge.control.snapshots import TriggerBinding
from fledge.control.store import Store, WorkflowRef
from fledge.core import WorkItem
from fledge.triggers import Comment

pytestmark = pytest.mark.anyio


def work_item(external_id: str) -> WorkItem:
    return WorkItem(
        external_id=external_id,
        title=f"Issue {external_id}",
        body="",
        url=f"https://example.test/{external_id}",
        repo_url="https://example.test/repo.git",
    )


@dataclass
class FakeTrigger:
    items: list[WorkItem] = field(default_factory=list)
    comments: dict[str, list[Comment]] = field(default_factory=dict)
    guidance_calls: list[tuple[str, datetime]] = field(default_factory=list)

    def check(self) -> None:
        pass

    def poll(self) -> list[WorkItem]:
        return self.items

    def fetch(self, ref: str) -> WorkItem:
        return work_item(ref)

    def poll_guidance(self, item_external_id: str, since: datetime) -> list[Comment]:
        self.guidance_calls.append((item_external_id, since))
        return self.comments.get(item_external_id, [])


class BrokenTrigger(FakeTrigger):
    def poll(self) -> list[WorkItem]:
        raise ConnectionError("ticket system is down")


CONFIG = Settings(image="fledge:test", api_url="http://fledge:8000", docker_network="n")


SNAPSHOT_HASH = "snapshot-under-test"


def bind(name: str, trigger: FakeTrigger) -> dict[str, TriggerBinding]:
    return {name: TriggerBinding(trigger, WorkflowRef(name, SNAPSHOT_HASH))}


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with Store(database_url) as store:
        await store.save_snapshot(SNAPSHOT_HASH, {})
        yield store


async def test_items_become_runs_once(store: Store) -> None:
    trigger = FakeTrigger(items=[work_item("1"), work_item("2")])
    poller = Poller(store, bind("ticket_to_pr", trigger), CONFIG)

    await poller.poll_once()
    await poller.poll_once()

    runs = await store.list_runs()
    assert sorted(run.external_id for run in runs) == ["1", "2"]
    assert {(run.workflow, run.snapshot_hash, run.origin) for run in runs} == {
        ("ticket_to_pr", SNAPSHOT_HASH, "poll")
    }


async def test_comments_on_active_runs_become_guidance_once(store: Store) -> None:
    trigger = FakeTrigger(items=[work_item("1")])
    poller = Poller(store, bind("ticket_to_pr", trigger), CONFIG)
    await poller.poll_once()
    [run] = await store.list_runs()

    trigger.comments["1"] = [
        Comment(external_id="c1", author="ann", body="use the v2 API"),
        Comment(external_id="c2", author="bob", body="and add a test"),
    ]
    await poller.poll_once()
    await poller.poll_once()

    guidance = await store.list_guidance(run.id)
    assert [(g.source, g.external_id, g.author, g.body) for g in guidance] == [
        ("trigger", "c1", "ann", "use the v2 API"),
        ("trigger", "c2", "bob", "and add a test"),
    ]
    assert trigger.guidance_calls[-1] == ("1", run.created_at)


async def test_finished_runs_are_not_polled_for_guidance(store: Store) -> None:
    trigger = FakeTrigger(items=[work_item("1")])
    poller = Poller(store, bind("ticket_to_pr", trigger), CONFIG)
    await poller.poll_once()
    await poller.poll_once()
    [run] = await store.list_runs()
    assert await store.finish_run(run.id, "succeeded")

    await poller.poll_once()

    assert trigger.guidance_calls == [("1", run.created_at)]


async def test_one_failing_trigger_does_not_block_another(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    healthy = FakeTrigger(items=[work_item("1")])
    poller = Poller(
        store,
        bind("broken", BrokenTrigger()) | bind("healthy", healthy),
        CONFIG,
    )

    await poller.poll_once()

    [run] = await store.list_runs()
    assert run.workflow == "healthy"
    assert "polling the trigger of workflow 'broken' failed" in caplog.text
