import hashlib
from collections.abc import AsyncIterator
from uuid import uuid4

import anyio
import pytest
from pydantic import JsonValue

from fledge.core import WorkItem
from fledge.store import (
    CallKind,
    Run,
    StepRun,
    Store,
    TerminalRunStatus,
    WorkflowRef,
    hash_token,
)

pytestmark = pytest.mark.anyio


def work_item(external_id: str = "42") -> WorkItem:
    return WorkItem(
        external_id=external_id,
        title=f"Issue {external_id}",
        body="Fix the thing",
        url=f"https://example.test/issues/{external_id}",
        repo_url="https://example.test/repo.git",
    )


SNAPSHOT_FILES = {"ticket.py": "# workflow source", "prompts/implement.md": "Fix"}
TICKET_TO_PR = WorkflowRef(name="ticket_to_pr", snapshot_hash="snapshot-under-test")
OTHER = WorkflowRef(name="other", snapshot_hash="snapshot-under-test")


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with Store(database_url) as store:
        await store.save_snapshot(TICKET_TO_PR.snapshot_hash, SNAPSHOT_FILES)
        yield store


async def make_run(store: Store, external_id: str = "42") -> Run:
    return await store.create_manual_run(TICKET_TO_PR, work_item(external_id))


async def test_snapshot_is_stored_once_per_hash(store: Store) -> None:
    await store.save_snapshot(TICKET_TO_PR.snapshot_hash, {"changed.py": ""})

    files = await store.get_snapshot_files(TICKET_TO_PR.snapshot_hash)

    assert files == SNAPSHOT_FILES
    with pytest.raises(LookupError):
        await store.get_snapshot_files("no-such-hash")


async def test_run_records_its_workflow_and_snapshot(store: Store) -> None:
    run = await make_run(store)

    assert run.workflow == "ticket_to_pr"
    assert run.snapshot_hash == TICKET_TO_PR.snapshot_hash


async def test_polled_run_is_created_once_per_item(store: Store) -> None:
    first = await store.create_polled_run(TICKET_TO_PR, work_item("1"))
    assert first is not None
    assert first.origin == "poll"
    assert first.status == "pending"
    assert first.work == work_item("1")
    assert await store.get_run(first.id) == first

    assert await store.create_polled_run(TICKET_TO_PR, work_item("1")) is None
    assert await store.create_polled_run(OTHER, work_item("1"))

    manual = await make_run(store, "2")
    assert manual.origin == "manual"
    assert await make_run(store, "2") != manual
    assert await store.create_polled_run(TICKET_TO_PR, work_item("2")) is None


async def test_concurrent_polls_create_one_run(store: Store) -> None:
    created: list[Run | None] = []

    async def poll() -> None:
        created.append(await store.create_polled_run(TICKET_TO_PR, work_item("7")))

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(poll)

    assert len([run for run in created if run is not None]) == 1


async def test_get_missing_run_returns_none(store: Store) -> None:
    assert await store.get_run(uuid4()) is None


async def test_run_listing_order(store: Store) -> None:
    runs = [await make_run(store, str(n)) for n in range(3)]
    assert await store.mark_run_running(runs[1].id)

    assert [run.id for run in await store.list_runs()] == [r.id for r in reversed(runs)]
    assert [run.id for run in await store.list_runs(limit=1)] == [runs[2].id]
    pending = await store.list_runs_with_status("pending")
    assert [run.id for run in pending] == [runs[0].id, runs[2].id]
    assert [run.id for run in await store.list_runs_with_status("running")] == [
        runs[1].id
    ]


@pytest.mark.parametrize(
    ("status", "error"),
    [("succeeded", None), ("failed", "step clone failed"), ("cancelled", None)],
)
async def test_run_lifecycle(
    store: Store, status: TerminalRunStatus, error: str | None
) -> None:
    run = await make_run(store)

    assert await store.mark_run_running(run.id)
    assert not await store.mark_run_running(run.id)
    assert await store.request_cancel(run.id)
    running = await store.get_run(run.id)
    assert running is not None
    assert running.status == "running"
    assert running.started_at is not None
    assert running.cancel_requested

    assert await store.finish_run(run.id, status, error)
    assert not await store.finish_run(run.id, "failed", "too late")
    assert not await store.request_cancel(run.id)
    finished = await store.get_run(run.id)
    assert finished is not None
    assert finished.status == status
    assert finished.error == error
    assert finished.finished_at is not None


async def test_pending_run_can_be_finished(store: Store) -> None:
    run = await make_run(store)
    assert await store.finish_run(run.id, "cancelled")
    assert not await store.mark_run_running(run.id)


async def make_call(
    store: Store, run: Run, index: int, kind: CallKind = "step"
) -> StepRun:
    return await store.create_step_run(
        run.id,
        index,
        kind,
        f"call{index}",
        function=f"workflows.ticket:call{index}",
        args={"n": index, "nested": {"items": [1, "two"]}},
        token_hash=hash_token(f"token-{index}"),
    )


async def test_step_runs_form_the_journal(store: Store) -> None:
    run = await make_run(store)
    second = await make_call(store, run, 1, "wait")
    first = await make_call(store, run, 0)
    assert (first.kind, first.name, first.function) == (
        "step",
        "call0",
        "workflows.ticket:call0",
    )
    assert first.args == {"n": 0, "nested": {"items": [1, "two"]}}
    assert (first.status, first.result, first.finished_at) == ("running", None, None)
    assert first.token_hash == hashlib.sha256(b"token-0").hexdigest()
    assert second.kind == "wait"
    assert await store.get_step_run(first.id) == first
    assert await store.get_step_run(uuid4()) is None
    assert [step.id for step in await store.list_step_runs(run.id)] == [
        first.id,
        second.id,
    ]

    result: JsonValue = {"branch": "fledge/42", "files": ["a.py"], "ok": True}
    assert await store.finish_step_run(first.id, "succeeded", result)
    assert await store.finish_step_run(second.id, "cancelled")
    # A result reported after the call was cancelled must not replace the outcome.
    assert not await store.finish_step_run(second.id, "succeeded", "late")

    done, cancelled = await store.list_step_runs(run.id)
    assert (done.status, done.result, done.error) == ("succeeded", result, None)
    assert done.finished_at is not None
    assert (cancelled.status, cancelled.result) == ("cancelled", None)


@pytest.mark.parametrize("result", ["text", 3, [1, 2], None])
async def test_step_result_may_be_any_json(store: Store, result: JsonValue) -> None:
    run = await make_run(store)
    call = await make_call(store, run, 0)

    assert await store.finish_step_run(call.id, "succeeded", result)

    finished = await store.get_step_run(call.id)
    assert finished is not None
    assert finished.result == result


async def test_failed_step_records_its_error(store: Store) -> None:
    run = await make_run(store)
    call = await make_call(store, run, 0)

    assert await store.finish_step_run(call.id, "failed", error="boom")

    failed = await store.get_step_run(call.id)
    assert failed is not None
    assert (failed.status, failed.error) == ("failed", "boom")


async def test_count_running_steps_spans_runs_and_skips_waits(store: Store) -> None:
    run = await make_run(store, "1")
    other = await make_run(store, "2")
    assert await store.count_running_steps() == 0

    step = await make_call(store, run, 0)
    await make_call(store, other, 0)
    await make_call(store, other, 1, "wait")
    assert await store.count_running_steps() == 2

    assert await store.finish_step_run(step.id, "succeeded")
    assert await store.count_running_steps() == 1


async def test_events_after_cursor(store: Store) -> None:
    run = await make_run(store)
    other = await make_run(store, "other")
    step = await make_call(store, run, 0)

    state = await store.append_event(run.id, "state", "run started")
    await store.append_event(other.id, "log", "unrelated")
    batch = await store.append_events(
        run.id, [("log", "cloning"), ("agent", "thinking"), ("log", "done")], step.id
    )
    assert await store.append_events(run.id, []) == []

    assert [event.message for event in batch] == ["cloning", "thinking", "done"]
    assert all(event.step_run_id == step.id for event in batch)
    assert state.step_run_id is None
    assert await store.list_events(run.id) == [state, *batch]
    assert await store.list_events(run.id, after=batch[0].id) == batch[1:]
    assert await store.list_events(run.id, after=batch[-1].id) == []


async def test_guidance(store: Store) -> None:
    run = await make_run(store)

    web = await store.add_guidance(run.id, "web", "alice", "use the v2 API")
    assert web is not None
    assert await store.add_guidance(run.id, "web", "alice", "use the v2 API")
    comment = await store.add_guidance(run.id, "trigger", "bob", "add tests", "c1")
    assert comment is not None
    assert comment.external_id == "c1"
    assert await store.add_guidance(run.id, "trigger", "bob", "again", "c1") is None

    claimed = await store.claim_guidance(run.id)
    assert [g.body for g in claimed] == [
        "use the v2 API",
        "use the v2 API",
        "add tests",
    ]
    assert all(g.delivered_at is not None for g in claimed)
    assert await store.claim_guidance(run.id) == []

    late = await store.add_guidance(run.id, "web", "alice", "also update docs")
    assert late is not None
    assert [g.id for g in await store.claim_guidance(run.id)] == [late.id]

    history = await store.list_guidance(run.id)
    assert [g.id for g in history] == [*(g.id for g in claimed), late.id]
    assert all(g.delivered_at is not None for g in history)
