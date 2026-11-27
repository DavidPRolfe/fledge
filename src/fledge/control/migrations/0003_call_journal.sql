-- Workflows are now functions replayed against a journal of their calls, so a
-- run no longer lists its steps up front and step_runs records every call.

-- Runs from before this migration cannot be replayed, so active ones are
-- ended. Their containers and workspace volumes are not cleaned up.
UPDATE runs
SET status = 'failed',
    error = 'workflows became functions; this run predates the change',
    finished_at = now()
WHERE status IN ('pending', 'running');

-- Running steps now count against the step limit, so those of the runs just
-- ended must not stay running.
UPDATE step_runs
SET status = 'cancelled',
    finished_at = now()
WHERE status = 'running';

ALTER TABLE runs DROP COLUMN steps;

-- The defaults only fill in rows from before this migration.
ALTER TABLE step_runs
    ADD COLUMN kind text NOT NULL DEFAULT 'step' CHECK (kind IN ('step', 'wait')),
    ADD COLUMN function text NOT NULL DEFAULT '',
    ADD COLUMN args jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN result jsonb,
    DROP COLUMN output;

ALTER TABLE step_runs
    ALTER COLUMN kind DROP DEFAULT,
    ALTER COLUMN function DROP DEFAULT;

-- The orchestrator counts running steps against the concurrency limit every tick.
CREATE INDEX step_runs_running ON step_runs (kind) WHERE status = 'running';
