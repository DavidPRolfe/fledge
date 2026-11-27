"""Postgres persistence for runs, step runs, events, guidance and workflow snapshots."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from psycopg import sql
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue, TypeAdapter

from fledge.core import WorkItem

RunOrigin = Literal["poll", "manual"]
RunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
TerminalRunStatus = Literal["succeeded", "failed", "cancelled"]
CallKind = Literal["step", "wait"]
StepStatus = Literal["running", "succeeded", "failed", "cancelled"]
TerminalStepStatus = Literal["succeeded", "failed", "cancelled"]
EventKind = Literal["log", "agent", "state"]
GuidanceSource = Literal["web", "trigger"]


@dataclass(frozen=True)
class WorkflowRef:
    """What a new run records about the workflow it runs."""

    name: str
    snapshot_hash: str


@dataclass(frozen=True)
class Run:
    id: UUID
    external_id: str
    origin: RunOrigin
    workflow: str
    title: str
    body: str
    url: str
    repo_url: str
    status: RunStatus
    cancel_requested: bool
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    snapshot_hash: str | None
    """None only for runs created before workflows were snapshotted."""

    @property
    def work(self) -> WorkItem:
        return WorkItem(
            external_id=self.external_id,
            title=self.title,
            body=self.body,
            url=self.url,
            repo_url=self.repo_url,
        )


@dataclass(frozen=True)
class StepRun:
    """One call a workflow made: a row of the run's journal."""

    id: UUID
    run_id: UUID
    step_index: int
    """The call's position among the calls of its run, from 0."""
    kind: CallKind
    name: str
    function: str
    """`module:qualname` of the step or wait function."""
    args: dict[str, JsonValue]
    status: StepStatus
    result: JsonValue
    error: str | None
    token_hash: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Event:
    id: int
    run_id: UUID
    step_run_id: UUID | None
    at: datetime
    kind: EventKind
    message: str


@dataclass(frozen=True)
class Guidance:
    id: UUID
    run_id: UUID
    source: GuidanceSource
    external_id: str | None
    author: str
    body: str
    created_at: datetime
    delivered_at: datetime | None


def _columns(row_type: type) -> sql.Composed:
    # class_row builds each dataclass from columns of the same names, so the
    # select list is derived from the fields to keep the two in step.
    return sql.SQL(", ").join(sql.Identifier(field.name) for field in fields(row_type))


_RUN = _columns(Run)
_STEP_RUN = _columns(StepRun)
_EVENT = _columns(Event)
_GUIDANCE = _columns(Guidance)
_FILES = TypeAdapter(dict[str, str])


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """Use as `async with Store(url) as store:`, or call open() and close()."""

    def __init__(self, conninfo: str) -> None:
        self._pool = AsyncConnectionPool(conninfo, open=False)

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def save_snapshot(self, snapshot_hash: str, files: Mapping[str, str]) -> None:
        """Store workflow source under its hash. A repeated hash is a no-op."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO workflow_snapshots (hash, files) VALUES (%s, %s) "
                "ON CONFLICT (hash) DO NOTHING",
                (snapshot_hash, Jsonb(dict(files))),
            )

    async def get_snapshot_files(self, snapshot_hash: str) -> dict[str, str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT files FROM workflow_snapshots WHERE hash = %s",
                (snapshot_hash,),
            )
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"no workflow snapshot {snapshot_hash}")
        return _FILES.validate_python(row[0])

    async def create_manual_run(self, workflow: WorkflowRef, work: WorkItem) -> Run:
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Run)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "INSERT INTO runs (external_id, origin, workflow, "
                    "snapshot_hash, title, body, url, repo_url) "
                    "VALUES (%s, 'manual', %s, %s, %s, %s, %s, %s) RETURNING {}"
                ).format(_RUN),
                (
                    work.external_id,
                    workflow.name,
                    workflow.snapshot_hash,
                    work.title,
                    work.body,
                    work.url,
                    work.repo_url,
                ),
            )
            return _one(await cur.fetchone())

    async def create_polled_run(
        self, workflow: WorkflowRef, work: WorkItem
    ) -> Run | None:
        """Insert a pending run unless any run exists for (workflow, external_id).

        Returns None when one already exists.
        """
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=class_row(Run)) as cur,
        ):
            # runs has no unique key on (workflow, external_id) because manual
            # launches may repeat an item, so concurrent polled inserts for the
            # same item are serialised with a transaction-scoped lock. The
            # INSERT starts after the lock is held and so sees any row a
            # competing transaction committed.
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (workflow.name, work.external_id),
            )
            await cur.execute(
                sql.SQL(
                    "INSERT INTO runs (external_id, origin, workflow, "
                    "snapshot_hash, title, body, url, repo_url) "
                    "SELECT %s, 'poll', %s, %s, %s, %s, %s, %s "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM runs WHERE workflow = %s AND external_id = %s) "
                    "RETURNING {}"
                ).format(_RUN),
                (
                    work.external_id,
                    workflow.name,
                    workflow.snapshot_hash,
                    work.title,
                    work.body,
                    work.url,
                    work.repo_url,
                    workflow.name,
                    work.external_id,
                ),
            )
            return await cur.fetchone()

    async def get_run(self, run_id: UUID) -> Run | None:
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Run)) as cur,
        ):
            await cur.execute(
                sql.SQL("SELECT {} FROM runs WHERE id = %s").format(_RUN), (run_id,)
            )
            return await cur.fetchone()

    async def list_runs(self, limit: int = 100) -> list[Run]:
        """Newest first."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Run)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT {} FROM runs ORDER BY created_at DESC, id DESC LIMIT %s"
                ).format(_RUN),
                (limit,),
            )
            return await cur.fetchall()

    async def list_runs_with_status(self, status: RunStatus) -> list[Run]:
        """Oldest first, so pending runs come out in the order they arrived."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Run)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT {} FROM runs WHERE status = %s ORDER BY created_at, id"
                ).format(_RUN),
                (status,),
            )
            return await cur.fetchall()

    async def mark_run_running(self, run_id: UUID) -> bool:
        """Move a pending run to running. False if it was not pending."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE runs SET status = 'running', started_at = now() "
                "WHERE id = %s AND status = 'pending'",
                (run_id,),
            )
            return cur.rowcount == 1

    async def finish_run(
        self, run_id: UUID, status: TerminalRunStatus, error: str | None = None
    ) -> bool:
        """Give a pending or running run its final status. False if it had one."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE runs SET status = %s, error = %s, finished_at = now() "
                "WHERE id = %s AND status IN ('pending', 'running')",
                (status, error, run_id),
            )
            return cur.rowcount == 1

    async def request_cancel(self, run_id: UUID) -> bool:
        """Flag a pending or running run for cancel. False if it already finished."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE runs SET cancel_requested = true "
                "WHERE id = %s AND status IN ('pending', 'running')",
                (run_id,),
            )
            return cur.rowcount == 1

    async def create_step_run(  # noqa: PLR0913 - one argument per column
        self,
        run_id: UUID,
        step_index: int,
        kind: CallKind,
        name: str,
        *,
        function: str,
        args: dict[str, JsonValue],
        token_hash: str,
    ) -> StepRun:
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(StepRun)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "INSERT INTO step_runs "
                    "(run_id, step_index, kind, name, function, args, token_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {}"
                ).format(_STEP_RUN),
                (run_id, step_index, kind, name, function, Jsonb(args), token_hash),
            )
            return _one(await cur.fetchone())

    async def get_step_run(self, step_run_id: UUID) -> StepRun | None:
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(StepRun)) as cur,
        ):
            await cur.execute(
                sql.SQL("SELECT {} FROM step_runs WHERE id = %s").format(_STEP_RUN),
                (step_run_id,),
            )
            return await cur.fetchone()

    async def list_step_runs(self, run_id: UUID) -> list[StepRun]:
        """In call order."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(StepRun)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT {} FROM step_runs WHERE run_id = %s ORDER BY step_index"
                ).format(_STEP_RUN),
                (run_id,),
            )
            return await cur.fetchall()

    async def finish_step_run(
        self,
        step_run_id: UUID,
        status: TerminalStepStatus,
        result: JsonValue = None,
        error: str | None = None,
    ) -> bool:
        """Record the step's outcome if it is still running.

        False means an outcome was already recorded (for example a cancel or
        timeout) and this one was discarded.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE step_runs SET status = %s, result = %s, error = %s, "
                "finished_at = now() WHERE id = %s AND status = 'running'",
                (status, Jsonb(result), error, step_run_id),
            )
            return cur.rowcount == 1

    async def count_running_steps(self) -> int:
        """Running calls of kind step across all runs: the step containers in use."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM step_runs "
                "WHERE kind = 'step' AND status = 'running'"
            )
            row = _one(await cur.fetchone())
        count: int = row[0]
        return count

    async def append_event(
        self,
        run_id: UUID,
        kind: EventKind,
        message: str,
        step_run_id: UUID | None = None,
    ) -> Event:
        [event] = await self.append_events(run_id, [(kind, message)], step_run_id)
        return event

    async def append_events(
        self,
        run_id: UUID,
        events: Sequence[tuple[EventKind, str]],
        step_run_id: UUID | None = None,
    ) -> list[Event]:
        """Insert events so their ids follow the given order. Returns them in that order."""
        if not events:
            return []
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Event)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "INSERT INTO events (run_id, step_run_id, kind, message) "
                    "SELECT %s, %s, e.kind, e.message "
                    "FROM unnest(%s::text[], %s::text[]) "
                    "WITH ORDINALITY AS e(kind, message, n) ORDER BY e.n "
                    "RETURNING {}"
                ).format(_EVENT),
                (
                    run_id,
                    step_run_id,
                    [kind for kind, _ in events],
                    [message for _, message in events],
                ),
            )
            return sorted(await cur.fetchall(), key=lambda event: event.id)

    async def list_events(
        self, run_id: UUID, after: int = 0, limit: int = 1000
    ) -> list[Event]:
        """Events with id greater than `after`, ascending."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Event)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT {} FROM events WHERE run_id = %s AND id > %s "
                    "ORDER BY id LIMIT %s"
                ).format(_EVENT),
                (run_id, after, limit),
            )
            return await cur.fetchall()

    async def add_guidance(
        self,
        run_id: UUID,
        source: GuidanceSource,
        author: str,
        body: str,
        external_id: str | None = None,
    ) -> Guidance | None:
        """Insert guidance. None if guidance with this external_id already exists."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Guidance)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "INSERT INTO guidance (run_id, source, external_id, author, body) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, external_id) DO NOTHING RETURNING {}"
                ).format(_GUIDANCE),
                (run_id, source, external_id, author, body),
            )
            return await cur.fetchone()

    async def claim_guidance(self, run_id: UUID) -> list[Guidance]:
        """Mark the run's undelivered guidance delivered and return it, oldest first."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Guidance)) as cur,
        ):
            # SKIP LOCKED keeps two concurrent claims from both returning a row.
            await cur.execute(
                sql.SQL(
                    "UPDATE guidance SET delivered_at = now() WHERE id IN ("
                    "SELECT id FROM guidance "
                    "WHERE run_id = %s AND delivered_at IS NULL "
                    "FOR UPDATE SKIP LOCKED) RETURNING {}"
                ).format(_GUIDANCE),
                (run_id,),
            )
            return sorted(
                await cur.fetchall(), key=lambda row: (row.created_at, str(row.id))
            )

    async def list_guidance(self, run_id: UUID) -> list[Guidance]:
        """Oldest first, delivered or not."""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=class_row(Guidance)) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT {} FROM guidance WHERE run_id = %s ORDER BY created_at, id"
                ).format(_GUIDANCE),
                (run_id,),
            )
            return await cur.fetchall()


def _one[T](row: T | None) -> T:
    if row is None:
        raise RuntimeError("expected the statement to return a row")
    return row
