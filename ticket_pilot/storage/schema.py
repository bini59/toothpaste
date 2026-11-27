"""Base SQLite tables for new and existing installations."""

# TP-91 project tables (also what _migrate_projects recreates). project_map: derived from config_projects (load_map);
# project_gid links tickets to repos: Asana gid or notion:<tp project id>, otherwise NULL.
PROJECT_TABLES = """
CREATE TABLE IF NOT EXISTS project_map (
  project_gid TEXT, project_name TEXT, repo TEXT, role TEXT, is_default INTEGER,
  confidence TEXT, note TEXT, project_id TEXT,
  PRIMARY KEY (project_id, repo)
);
CREATE TABLE IF NOT EXISTS config_projects (
  id TEXT PRIMARY KEY, pos INTEGER, name TEXT, source TEXT, asana_gid TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS config_project_repos (
  project_id TEXT, repo TEXT, pos INTEGER, role TEXT, is_default INTEGER, PRIMARY KEY (project_id, repo)
);
"""

SCHEMA = PROJECT_TABLES + """
CREATE TABLE IF NOT EXISTS notion_connections (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, workspace_name TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notion_status_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_gid TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
  data_source_id TEXT NOT NULL, stage TEXT NOT NULL, last_error TEXT, next_attempt_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tickets (
  gid TEXT PRIMARY KEY,
  key TEXT,                -- Asana "ID" custom field (CRSC-340) or AS-<gid>
  name TEXT,
  url TEXT,
  project_gid TEXT,
  project_name TEXT,
  section TEXT,
  parent_gid TEXT,
  parent_name TEXT,
  due_on TEXT,
  notes TEXT,
  tags TEXT,
  has_tag INTEGER DEFAULT 0,
  deps_json TEXT,          -- [{gid,name,completed}]
  open_deps INTEGER DEFAULT 0,
  completed INTEGER DEFAULT 0,
  assigned INTEGER DEFAULT 1,
  asana_modified_at TEXT,
  first_seen TEXT,
  last_seen TEXT,
  stage TEXT,
  repo_override TEXT,
  skip INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS repos (
  name TEXT PRIMARY KEY, orca_repo_id TEXT, path TEXT, github TEXT, description TEXT
);
-- settings (TP-49): the source of truth for what config/settings.json + config/project_map.json used to hold.
-- config_global: top-level keys; ns 'settings' (deploy without its repos) or 'project_map' (comments). v = JSON
CREATE TABLE IF NOT EXISTS config_global (ns TEXT, k TEXT, v TEXT, pos INTEGER, PRIMARY KEY (ns, k));
-- one row per repo: map entry (info = orca_repo_id/path/github/description JSON; NULL = deploy rule only), checks
-- ([{name, cmd, dir?, off?, reason?}]; off = kept with a reason but not run), ci, review overrides, deploy rule JSON.
-- map_pos / deploy_pos keep the JSON order (deploy order = release train order)
CREATE TABLE IF NOT EXISTS config_repos (
  name TEXT PRIMARY KEY, map_pos INTEGER, info TEXT, checks TEXT, ci INTEGER, review TEXT,
  deploy_pos INTEGER, deploy TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS config_hidden (asana_gid TEXT PRIMARY KEY, pos INTEGER, name TEXT);
-- every settings change: actor dashboard | cli | import, scope global | <repo> | project:<id>, key = dotted path
CREATE TABLE IF NOT EXISTS settings_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, scope TEXT, key TEXT, old TEXT, new TEXT
);
-- 설정 page "base 브랜치에서 시험 실행": detached `tp check --repo --ref --record <id>`
CREATE TABLE IF NOT EXISTS config_check_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, ref TEXT, sha TEXT, status TEXT, results TEXT, log_path TEXT,
  pid INTEGER, error TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_gid TEXT,
  repo TEXT,
  worktree_id TEXT,
  worktree_name TEXT,
  worktree_path TEXT,
  branch TEXT,
  terminal TEXT,
  ws_status TEXT,
  ws_comment TEXT,
  state TEXT,              -- active | finished | abandoned
  origin TEXT,             -- dispatch | adopt
  created_at TEXT,
  updated_at TEXT,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS prs (
  url TEXT PRIMARY KEY,
  run_id INTEGER,
  ticket_gid TEXT,
  repo TEXT,
  github TEXT,
  number INTEGER,
  title TEXT,
  state TEXT,
  is_draft INTEGER,
  head_sha TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_url TEXT,
  ticket_gid TEXT,
  head_sha TEXT,
  verdict TEXT,            -- approve | changes | blocked
  summary TEXT,
  body TEXT,
  comment_url TEXT,
  posted INTEGER DEFAULT 0,
  created_at TEXT,
  rework_at TEXT           -- when a 'changes' verdict was sent back to the agent (tp dispatch)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, ticket_gid TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS metric_markers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_agent_runs (
  log TEXT PRIMARY KEY, handle TEXT, kind TEXT NOT NULL, ticket_gid TEXT,
  started_at REAL, finished_at REAL, has_ws_set INTEGER, exit_event INTEGER,
  tool TEXT, tokens INTEGER, tool_calls INTEGER,
  help_calls INTEGER, dev_calls INTEGER, wait_calls INTEGER
);
CREATE INDEX IF NOT EXISTS metric_agent_pending ON metric_agent_runs(finished_at,started_at DESC);
CREATE TABLE IF NOT EXISTS metric_review_starts (
  log TEXT NOT NULL, at REAL NOT NULL, ticket_gid TEXT NOT NULL,
  PRIMARY KEY (log, at)
);
CREATE TABLE IF NOT EXISTS metric_review_tokens (
  log TEXT NOT NULL, ticket_gid TEXT NOT NULL, started_at REAL NOT NULL, tokens INTEGER,
  PRIMARY KEY (log, ticket_gid)
);
-- TP-105 수신함 alerts the owner has to look at (events is the log): kind review | ms_ready; read_at NULL = unread
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, ticket_gid TEXT, title TEXT, body TEXT, href TEXT, read_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- TP-152 로컬망 공유 logins (lan.py): id_hash = sha256 of the cookie (the cookie itself is never stored), org = the
-- org checked at login, checked_at = last org membership check
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, login TEXT, org TEXT, created_at TEXT, last_seen TEXT, checked_at TEXT, ip TEXT
);
CREATE TABLE IF NOT EXISTS staging_diffs (
  repo TEXT PRIMARY KEY,
  github TEXT,
  prod_branch TEXT,
  staging_branch TEXT,
  staging_head TEXT,
  ahead INTEGER,
  hotfixes INTEGER,          -- non-merge commits in prod that staging lacks (staging -> prod may conflict)
  via_ahead INTEGER,         -- via repos: commits in main that production lacks
  pipeline TEXT,             -- staging pipeline status for staging_head (Succeeded/InProgress/Failed/...) or NULL
  verdict TEXT,              -- ready | building | broken | none | error
  oldest_at TEXT,            -- committer date of the oldest commit not in prod
  items_json TEXT,           -- [{kind: ticket|pr|commit|merge, ...}]
  error TEXT,
  checked_at TEXT,
  note_md TEXT, note_level TEXT, note_head TEXT, note_at TEXT
);
-- ^ diffs.py: production vs staging per repo (Orca automation "ticket-pilot: staging diff")
CREATE TABLE IF NOT EXISTS cleanups (
  path TEXT PRIMARY KEY,     -- ticket worktree path (runs.worktree_path)
  ticket_gid TEXT, key TEXT, repo TEXT, branch TEXT,
  heads TEXT,                -- sorted head shas of the merged PRs that were checked
  prs_json TEXT,             -- [{number, url, merge_sha, head_sha, live: merge|cherry|patch|agent|null, signals}]
  prod_sha TEXT, prod_note TEXT,
  status TEXT,               -- waiting | ambiguous | kept | removing | removed | error
  reason TEXT,
  agent_verdict TEXT, agent_note TEXT, agent_sha TEXT,
  checked_at TEXT, removed_at TEXT
);
-- ^ cleanup.py: remove ticket worktrees once production has their change (Orca automation "ticket-pilot: worktree cleanup")
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_url TEXT, ticket_gid TEXT, head_sha TEXT,
  name TEXT,               -- project_map.json repos.<name>.checks[].name, or the GitHub check name for "ci" repos
  cmd TEXT,
  status TEXT,             -- running | pass | fail | flaky (failed, passed on the rerun) | timeout | error | ci_pending
  exit_code INTEGER,
  duration_s REAL,
  log_path TEXT,           -- data/checks/<repo>-<n>-<sha7>-<name>.log, or the GitHub details URL
  created_at TEXT,
  finished_at TEXT,
  attempt INTEGER DEFAULT 1,   -- 2 = rerun after tp could not run the checks (error)
  UNIQUE (pr_url, head_sha, name)   -- also the lock: one run per PR head
);
-- ^ checks.py: tp's own checks on the PR head (tp check); gate the review queue and the dashboard approval
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,                -- KST date of the release train
  repo TEXT,               -- repos.name ('' for a "nothing to release" marker)
  github TEXT,
  status TEXT,             -- pending | deploying | done | aborted | failed | empty
  reason TEXT,
  prs_json TEXT,           -- [{url, number, key, title}]
  release_pr_url TEXT,     -- the staging -> main PR
  merge_sha TEXT,
  started_at TEXT,
  merged_at TEXT,
  finished_at TEXT
);
-- chat.py (프로젝트 page 채팅): the owner's conversations with a headless agent in a repo's checkout. session = the
-- conversation ID the next turn resumes; tool = the agent it belongs to (a tool change starts over).
-- project_id (TP-91, added by _migrate) = the project it belongs to; repo = the checkout its session runs in
CREATE TABLE IF NOT EXISTS chat_conversations (
  id TEXT PRIMARY KEY, repo TEXT, title TEXT, session TEXT, tool TEXT, created_at TEXT, updated_at TEXT
);
-- one row per message or compaction. A NULL text marks a running process (handle = runner handle).
-- A finished compaction stores the handoff in text, which chat.messages displays as a divider.
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, role TEXT, text TEXT, ts TEXT,
  handle TEXT, failed INTEGER DEFAULT 0, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS chat_messages_conv ON chat_messages(conversation_id, id);
"""
