-- Runs and their evidence. Users live in neon_auth (managed by Neon); no foreign key into that schema.
CREATE TABLE runs (
  id          text PRIMARY KEY,
  user_id     text,
  instance_id text NOT NULL,
  pr          text NOT NULL CHECK (pr IN ('gold', 'none', 'diff')),
  status      text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
  verdict     text,
  reason      text,
  seconds     real,
  tokens      integer,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  evidence    jsonb
);

-- "Your receipts": WHERE user_id = $1 ORDER BY started_at DESC, id DESC, keyset-paged on (started_at, id)
CREATE INDEX runs_user_started ON runs (user_id, started_at DESC, id DESC);
