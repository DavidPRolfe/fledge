-- Workflows now carry their own trigger, so a run is identified by workflow
-- rather than trigger, and records the snapshot of workflow code it runs.

CREATE TABLE workflow_snapshots (
    hash text PRIMARY KEY,
    -- Source text by path relative to the workflows directory.
    files jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Runs from before this migration have no snapshot to run, so active ones are
-- ended. Their containers and workspace volumes are not cleaned up.
UPDATE runs
SET status = 'failed',
    error = 'workflow definitions moved to code; this run predates the change',
    finished_at = now()
WHERE status IN ('pending', 'running');

DROP INDEX runs_trigger_external_id;
ALTER TABLE runs DROP COLUMN trigger;

-- NULL only for runs from before this migration.
ALTER TABLE runs ADD COLUMN snapshot_hash text REFERENCES workflow_snapshots (hash);
ALTER TABLE runs ADD COLUMN steps text[];

CREATE INDEX runs_workflow_external_id ON runs (workflow, external_id);
