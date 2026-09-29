-- GitHub App installations, who may use them, per-repo Auto-check, and PR runs.
CREATE TABLE github_installations (
  id            bigint PRIMARY KEY,          -- GitHub installation id
  account_login text NOT NULL,
  account_type  text NOT NULL,               -- User | Organization
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Filled only from GitHub's GET /user/installations for that user, never from a redirect parameter.
CREATE TABLE github_installation_users (
  installation_id bigint NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,
  user_id         text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (installation_id, user_id)
);

CREATE TABLE github_repo_settings (
  repo_id         bigint PRIMARY KEY,        -- GitHub repository id
  installation_id bigint NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,
  full_name       text NOT NULL,
  auto_check      boolean NOT NULL DEFAULT false
);

ALTER TABLE runs DROP CONSTRAINT runs_pr_check,
  ADD CONSTRAINT runs_pr_check CHECK (pr IN ('gold', 'none', 'diff', 'github'));
ALTER TABLE runs ADD COLUMN repo text, ADD COLUMN pr_number integer, ADD COLUMN head_sha text,
  ADD COLUMN check_run_id bigint;
CREATE INDEX runs_repo_pr ON runs (repo, pr_number, started_at DESC);
