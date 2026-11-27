CREATE TABLE runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger text NOT NULL,
    external_id text NOT NULL,
    origin text NOT NULL CHECK (origin IN ('poll', 'manual')),
    workflow text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    url text NOT NULL,
    repo_url text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    cancel_requested boolean NOT NULL DEFAULT false,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE INDEX runs_trigger_external_id ON runs (trigger, external_id);
CREATE INDEX runs_status ON runs (status);

CREATE TABLE step_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs (id),
    step_index integer NOT NULL,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'succeeded', 'failed', 'cancelled')
    ),
    output jsonb NOT NULL DEFAULT '{}',
    error text,
    token_hash text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (run_id, step_index)
);

CREATE TABLE events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id),
    step_run_id uuid REFERENCES step_runs (id),
    at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL CHECK (kind IN ('log', 'agent', 'state')),
    message text NOT NULL
);

CREATE INDEX events_run_id ON events (run_id, id);

CREATE TABLE guidance (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs (id),
    source text NOT NULL CHECK (source IN ('web', 'trigger')),
    external_id text,
    author text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    UNIQUE (run_id, external_id)
);
