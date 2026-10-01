-- Blind tests worth reusing: a test depends only on the issue and the unpatched code, never on the PR.
CREATE TABLE blind_tests (
  key        text PRIMARY KEY,               -- sha256 of repo, base and issue text
  repo       text NOT NULL,
  run_id     text NOT NULL,                  -- the check whose writer wrote it
  test_code  text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
