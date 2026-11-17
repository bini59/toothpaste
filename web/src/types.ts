export interface Ticket {
  gid: string;
  key: string;
  name: string;
  url: string;
  project_gid: string | null;
  project_name: string | null;
  section: string | null;
  parent_gid: string | null;
  parent_name: string | null;
  due_on: string | null;
  tags: string | null;
  has_tag: number;
  deps_json: string | null; // [{gid, name, completed, landed}]; landed = feature base the dep's PR merged into
  open_deps: number;
  completed: number;
  assigned: number;
  stage: string;
  repo_override: string | null;
  skip: number;
  approve_requested_at: string | null;
  approve_error: string | null;
  reply: string | null;
  /** The owner's reason for approving although tp checks did not pass; applies to check_override_sha only */
  check_override: string | null;
  check_override_sha: string | null;
  /** The owner's reason for approving although the AI review said blocked/changes; applies to review_override_sha only */
  review_override: string | null;
  review_override_sha: string | null;
  /** the Asana parent has the milestone tag (milestone.py) */
  in_milestone: number | null;
  /** milestone tickets: when the owner pressed 마일스톤 작업 완료 (cleared when a subtask is in progress again) */
  ms_done_at: string | null;
  /** when the stage became done/closed (cleared on reopen); the board hides ones older than DONE_KEEP_DAYS */
  done_at: string | null;
  asana_modified_at: string | null;
}
export interface Run {
  id: number;
  ticket_gid: string;
  repo: string;
  worktree_name: string;
  worktree_path: string | null;
  branch: string | null;
  ws_status: string | null;
  ws_comment: string | null;
  state: string | null;
  origin: string | null;
  created_at: string;
  updated_at: string;
}
export interface PR {
  ticket_gid: string;
  repo: string;
  url: string;
  number: number;
  title: string | null;
  state: string;
  is_draft: number;
  head_sha: string | null;
  base: string | null;
  updated_at: string | null;
  merged_at: string | null;
  release_id: number | null;
  /** track.py: when the merge was seen in what production last deployed (outside the #03 train) */
  prod_at: string | null;
  prod_note: string | null;
  /** GitHub mergeability vs its base; null = not computed yet (scan re-reads it every 5 min) */
  mergeable: "MERGEABLE" | "CONFLICTING" | null;
  /** open PRs only: tp's own checks of the head commit (checks.py) */
  checks?: PrChecks;
}
export interface CheckRow {
  id: number;
  name: string;
  cmd: string;
  status: "running" | "pass" | "fail" | "flaky" | "timeout" | "error" | "ci_pending";
  exit_code: number | null;
  duration_s: number | null;
  /** data/checks/... log, or the GitHub details URL for CI repos */
  log_path: string | null;
  created_at: string;
  finished_at: string | null;
}
export interface PrChecks {
  /** none = the repo has no checks (not gated) */
  state: "none" | "pending" | "pass" | "fail" | "error";
  rows: CheckRow[];
  /** why the approval is blocked (release.approval_target), or null */
  gate: string | null;
}
export interface Review {
  id: number;
  ticket_gid: string;
  pr_url: string;
  verdict: "approve" | "changes" | "blocked";
  summary: string;
  body: string;
  comment_url: string | null;
  head_sha: string | null;
  created_at: string;
  /** null = AI review, "check" = tp check failure sent back to the agent */
  source: "check" | null;
  /** set when this review was sent back to the agent as a fix round (pipeline.rework_rounds) */
  rework_at: string | null;
}
export interface Repo {
  name: string;
  orca_repo_id: string;
  path: string;
  github: string | null;
  description: string | null;
  /** read-only settings the 작업실 factory draws (server.factory_repo, TP-127) */
  agent: { tool: string; model: string | null };
  checks: string[];
  ci: boolean;
  deploy: Pick<Rule, "train" | "modes" | "merge"> | null;
  max_rework: number;
}
export interface ProjectMapRow {
  /** tp project id (TP-91) */
  project_id: string;
  /** its Asana project, null when it has none */
  project_gid: string | null;
  project_name: string;
  repo: string | null;
  role: string | null;
  is_default: number;
  confidence: string | null;
  note: string | null;
}
export interface EventRow {
  id: number;
  ts: string;
  kind: string;
  message: string;
  actor?: string | null; // TP-152: GitHub login (LAN) or owner_name (127.0.0.1); null = tp itself
}
/** 로컬망 공유 (TP-152) */
export interface LanMe {
  local: boolean;
  login: string | null;
  actor: string | null;
  /** Docker mode (TP-164): everyone logs in; admin = in TP_ADMINS */
  docker?: boolean;
  admin?: boolean;
  /** TP-166: TP_PUBLIC_URL, the tunnel's https address (Docker mode) */
  public_url?: string | null;
}
/** 설정 → 고급 "외부 공개" (TP-166): the cloudflared container's state */
export interface TunnelStatus {
  /** TP-172: false in host mode, the only field then */
  docker?: false;
  public_url: string | null;
  running: boolean;
  connections: number;
}
export interface LanStatus {
  enabled: boolean;
  org: string;
  /** may change it and force logouts: 127.0.0.1, in Docker mode an admin */
  local: boolean;
  docker?: boolean;
  address: string | null;
  sessions: { id: number; login: string; org: string; created_at: string; last_seen: string; ip: string }[];
}
export interface State {
  meta: {
    last_sync: string | null;
    last_scan: string | null;
    settings: { asana_tag: string; [k: string]: unknown };
    release_hold: boolean;
    next_release_at: string;
    /** per-repo deploy settings, normalized (core.repo_rules) */
    rules: Record<string, Rule>;
    modes: Record<Mode, string>;
    last_diff_scan: string | null;
    /** usage.py cache; five_hour/seven_day/fetched_at are the last successful read, error the last attempt's */
    claude_usage: ClaudeUsage | null;
    /** codex_usage.py cache, same shape rules: buckets/fetched_at last successful, error last attempt's */
    codex_usage: CodexUsage | null;
    /** server.version: git describe of the checkout; update = a newer release tag tp tick saw, else null (TP-140) */
    version: { current: string; update: string | null };
  };
  staging_diffs: StagingDiff[];
  tickets: Ticket[];
  runs: Run[];
  prs: PR[];
  reviews: Review[];
  repos: Repo[];
  project_map: ProjectMapRow[];
  events: EventRow[];
  releases: Release[];
  dev_servers?: DevServer[];
}
/** a milestone dev server (devserver.py, meta dev_servers) */
export interface DevServer {
  gid: string;
  repo: string;
  path: string;
  port: number;
  pid: number;
  log: string;
  cmd: string;
  started: string;
  alive: boolean;
  listening: boolean;
  /** log tail while it is not (yet) listening */
  tail: string;
}
export interface DevProc {
  pid: number | null;
  cmd: string;
  cwd: string | null;
}
export interface DevConflict {
  port: number;
  busy: DevProc[];
  free: number;
}
/** /api/dev/check: worktree, .env check, command and port, who listens on it */
export interface DevCheck {
  running?: DevServer;
  path: string;
  repo: string;
  notes: string[];
  env: { files: string[]; copied: string[]; missing: Record<string, string[]>; warnings: string[] };
  cmd: string;
  how: string;
  port: number;
  /** the dev script hard-codes its port */
  fixed: boolean;
  busy: DevProc[];
  /** another milestone dev server of ours holds the port */
  ours: boolean;
  free?: number;
}
export interface DiffItem {
  kind: "ticket" | "pr" | "commit" | "merge";
  sha: string;
  date: string;
  author: string;
  number?: number;
  key?: string;
  title?: string;
  url?: string;
  stage?: string;
  msg?: string;
  /** already in production through a cherry-pick */
  live?: boolean;
}
/** diffs.py: production vs staging per repo (Orca automation "ticket-pilot: staging diff") */
export interface StagingDiff {
  repo: string;
  github: string;
  prod_branch: string;
  staging_branch: string;
  staging_head: string | null;
  ahead: number | null;
  hotfixes: number | null;
  via_ahead: number | null;
  pipeline: string | null;
  verdict: "ready" | "building" | "broken" | "none" | "error";
  oldest_at: string | null;
  items_json: string | null;
  error: string | null;
  checked_at: string | null;
  note_md: string | null;
  note_level: "ok" | "check" | "hold" | null;
  note_head: string | null;
  note_at: string | null;
}
export type Mode = "staging" | "direct" | "promote" | "all" | "preview";
export interface Rule {
  staging: { branch: string; pipeline?: string } | null;
  prod: { branch: string; pipeline?: string; workflow?: string; release?: boolean; ecs?: unknown; smoke?: string[] };
  via: string | null;
  target: string;
  merge: "merge" | "squash";
  modes: Mode[];
  train: boolean;
  version?: string;
  preview_label?: string;
  _note?: string;
}
export interface Release {
  id: number;
  day: string;
  repo: string;
  github: string | null;
  /** train rows: pending | deploying | done | aborted | failed | empty; dashboard deploys add requested | running | cancelled */
  status: "requested" | "running" | "pending" | "deploying" | "done" | "aborted" | "failed" | "empty" | "cancelled";
  /** null = weekday train */
  mode: Mode | null;
  ticket_gid: string | null;
  step: string | null;
  version: string | null;
  requested_at: string | null;
  reason: string | null;
  prs_json: string | null;
  release_pr_url: string | null;
  merge_sha: string | null;
  started_at: string | null;
  merged_at: string | null;
  finished_at: string | null;
}
export interface AutomationsResponse {
  captured_at?: string;
  error?: string;
  automations?: { id: string; name: string; rrule: string; enabled: boolean; nextRunAt?: number | string; lastRunAt?: number | string }[];
  items?: { automationId: string; usageSummary?: { lastRunStatus?: string; knownRuns?: number; estimatedCostUsd?: number } }[];
}
/** 시스템 자동화 page (ticket_pilot/sysjobs.py): launchd / cron / Codex app automations on this Mac */
export interface SystemJobFile {
  label: string;
  path: string;
  size: number | null;
  mtime: string | null;
}
export interface SystemJob {
  id: string;
  source: "launchd" | "cron" | "codex";
  name: string;
  file: string;
  command: string;
  schedule: string;
  spec?: string | null;
  status: "running" | "idle" | "failed" | "disabled" | "not_loaded" | "scheduled" | "paused" | "error";
  pid?: number | null;
  last_exit?: number | null;
  /** updaters/helpers installed by apps; hidden unless asked */
  app_item: boolean;
  logs: SystemJobFile[];
  error?: string;
}
export interface SystemJobsResponse {
  jobs: SystemJob[];
  errors: string[];
  collected_at: string;
}
export interface SystemJobLog extends SystemJobFile {
  log: string;
  truncated: boolean;
  error?: string;
}
export interface UsageWindow {
  /** percent used, 0-100 */
  utilization: number;
  resets_at: string | null;
}
export interface ClaudeUsage {
  five_hour?: UsageWindow;
  seven_day?: UsageWindow;
  fetched_at?: string;
  checked_at: string;
  error: string | null;
}
/** codex_usage.normalize: percent left in each limit window, unix seconds reset */
export interface CodexWindow {
  window: "primary" | "secondary";
  windowDurationMins: number | null;
  remainingPercent: number | null;
  resetAt: number | null;
}
export interface CodexBucket {
  id: string;
  name: string;
  windows: CodexWindow[];
}
export interface CodexUsage {
  buckets?: CodexBucket[];
  fetched_at?: string;
  checked_at: string;
  error: string | null;
}

// ---------------------------------------------------------------- 설정 page (GET /api/config, ticket_pilot/config.py)
export interface CheckCmd {
  name: string;
  cmd: string;
  dir?: string;
  /** switched off with a reason: kept, not run */
  off?: boolean;
  reason?: string;
}
export interface DeployRule {
  modes?: string[];
  merge?: string;
  train?: boolean;
  staging?: { branch: string; pipeline?: string } | null;
  prod: { branch: string; pipeline?: string; workflow?: string; release?: boolean; ecs?: { cluster: string; service: string }; smoke?: string[] };
  via?: string | null;
  version?: string | null;
  pr_into_staging?: boolean;
  preview_label?: string;
  _note?: string;
  [k: string]: unknown;
}
export interface RepoLink {
  /** tp project id (TP-91); a link with only asana_gid goes to (or creates) the project of that Asana project */
  project_id?: string;
  asana_gid?: string;
  project?: string | null;
  role: string | null;
  default: boolean;
}
export interface AgentConf {
  tool?: string | null;
  model?: string | null;
  effort?: string | null;
  ultracode?: boolean | null;
}
export interface RepoConf {
  path: string | null;
  github: string | null;
  orca_repo_id: string | null;
  description: string | null;
  /** shell script run in each new ticket worktree before the agent starts (deps, .env) */
  setup?: string | null;
  /** branch new ticket worktrees start from when the repo has no feature base */
  worktree_base?: string | null;
  /** false = no automatic worktree prep (.env copy + pnpm/yarn/npm install) */
  auto_setup?: boolean | null;
  /** dev server of the 마일스톤 page: command ({port} = chosen port) and port; empty = auto-detect */
  dev?: { cmd?: string | null; port?: number | null } | null;
  /** repos whose production may need this repo's staging change first (staging diff review) */
  depends_on?: string[] | null;
  /** this repo's ticket agents; empty values = global agent tool / the CLI's own config */
  agent?: AgentConf | null;
  checks: CheckCmd[];
  ci: boolean;
  review: { max_rework_rounds?: number | string | null };
  deploy: DeployRule | null;
  links: RepoLink[];
  [k: string]: unknown;
}
/** /api/github/status: tp's own GitHub token (TP-112); the token itself never leaves the server */
export type GitHubStatus = { connected: boolean; login?: string; scopes?: string[]; error?: string };
/** /api/github/device (TP-143): the code to enter at verification_uri; the device_code stays on the server */
export type GitHubDevice = { user_code: string; verification_uri: string; expires_in: number; interval: number };
export type AsanaStatus = {
  connected: boolean; name?: string; email?: string; env?: boolean; error?: string;
  workspaces?: { gid: string; name: string }[]; workspace?: string; // TP-148: workspaces of the PAT, the chosen gid
};
export type GlobalConf = Record<string, unknown> & {
  max_active_runs: number;
  agent: string;
  /** per-role agent cards (TP-168); a missing role follows `agent` */
  agents: Partial<Record<"work" | "review" | "check", AgentConf>>;
  worktree_root: string;
  clone_root: string;
  branch_prefix: string;
  agent_max_min: number;
  agent_asana_write: boolean;
  owner_name: string;
  theme: string;
  review: { max_rework_rounds: number; [k: string]: unknown };
  check: { timeout_min: number; max_parallel: number; [k: string]: unknown };
  release: { weekdays: number[]; at: string; window_min: number; timezone: string; staging_branch: string; main_branch: string; [k: string]: unknown };
  deploy: { deploy_timeout_min: number; [k: string]: unknown };
  update: { auto: boolean; check_min: number };
  github_issues: Record<string, string>;
  hidden_projects: Record<string, string>;
};
export type ProjectSource = "asana" | "github" | "none";
/** project.<id> (TP-91): a project's own name, where its tickets come from, and its repos */
export interface ProjectConf {
  name: string;
  source: ProjectSource;
  asana_gid: string | null;
  repos: { repo: string; role: string | null; default?: boolean }[];
  [k: string]: unknown;
}
/** /api/agents/accounts (TP-168): claude / codex install and login; account = labels only (email, plan), never a token.
 * method: what the agents use (TP-179); saved: the credential tp holds for claude; admin: may press login / logout */
export type AgentLoginMethod = "cli" | "oauth_token" | "api_key";
export type AgentAccount = { installed: boolean; version: string | null; logged_in: boolean; account: string[];
  method: AgentLoginMethod | null; saved: AgentLoginMethod | null };
export type AgentAccounts = { docker: boolean; admin: boolean; tools: Record<string, AgentAccount> };
/** /api/agents/codex/login (TP-179): codex login --device-auth; code = the one-time code shown to the owner */
export type CodexLogin = { state: "idle" | "pending" | "done" | "failed" | "expired"; url?: string; code?: string; expires_in?: number; error?: string };
export interface HistoryRow {
  id: number;
  ts: string;
  actor: string; // dashboard | cli | import, or who did it on the dashboard (TP-152)
  scope: string;
  key: string;
  old: unknown;
  new: unknown;
}
export interface TrialResult {
  name: string;
  cmd: string;
  status: string;
  exit_code: number | null;
  duration_s: number;
  log: string;
}
export interface TrialRun {
  id: number;
  repo: string;
  ref: string;
  sha: string | null;
  status: "running" | "pass" | "fail" | "error";
  results: TrialResult[];
  error: string | null;
  started_at: string;
  finished_at: string | null;
}
export interface ConfigChange {
  scope: string;
  key: string;
  old: unknown;
  new: unknown;
}
export interface ConfigState {
  config: { global: GlobalConf; repo: Record<string, RepoConf>; project: Record<string, ProjectConf> };
  read_only: string[];
  modes: string[];
  merges: string[];
  versions: string[];
  agents: string[];
  agent_efforts: Record<string, string[]>;
  /** model choices per agent tool: codex from `codex debug models`, claude aliases */
  agent_models?: Record<string, { id: string; name: string; efforts: string[]; default_effort: string | null }[]>;
  repo_info: Record<string, { path_ok: boolean; review_md: boolean; base_ref: string | null }>;
  check_runs: Record<string, TrialRun>;
  history: HistoryRow[];
  /** Asana projects known from the settings and synced tickets: gid -> name */
  projects: Record<string, string>;
  imported_at: string | null;
  backups: { dir: string; count: number };
}


// ---------------------------------------------------------------- 프로젝트 page (chat.py)
export interface ProjectRepo {
  name: string;
  role: string | null;
  default: boolean;
  description: string;
  github: string;
  path: string;
  path_ok: boolean;
}
/** a project (TP-91): 1..N repos, default repo first; unsorted = 미분류 (the repos no project links) */
export interface Project {
  id: string;
  name: string;
  source: ProjectSource | null;
  asana_gid: string | null;
  unsorted: boolean;
  repos: ProjectRepo[];
  /** the default repo: the chat runs in its checkout (description/github/path/path_ok/agent are its values) */
  repo: string | null;
  description: string;
  github: string;
  path: string;
  path_ok: boolean;
  agent: string; // e.g. "codex", "claude opus/high"
  conversations: number;
  running: number;
  /** finished replies not seen yet (TP-103) */
  unread: number;
  last_at: string | null;
}
export interface ChatConversation {
  id: string;
  repo: string;
  project_id: string | null;
  title: string;
  tool: string | null;
  created_at: string;
  updated_at: string;
  running?: number;
  unread?: number;
  /** the last assistant message seen (chat_conversations.read_id) */
  read_id?: number | null;
  preview?: string;
  session?: string | null;
}
/** 수신함 (/api/chat/inbox): a conversation with unread replies; preview/at/failed are of its newest reply */
export interface InboxItem {
  id: string;
  title: string;
  project_id: string;
  project: string;
  unread: number;
  last_id: number;
  preview: string;
  failed: boolean;
  at: string;
}
/** 수신함 alert (TP-105, notifications table): review = AI review of an ordinary ticket, ms_ready = 마일스톤 작업 완료 needed */
export interface InboxNotification {
  id: number;
  ts: string;
  kind: "review" | "ms_ready" | "deploy" | "blocked";
  ticket_gid: string | null;
  title: string;
  body: string;
  /** a dashboard hash (#milestones/<parent gid>, #projects/<id>/board) or a URL */
  href: string;
  /** review alerts: the PR */
  pr_url?: string;
  ticket_key: string | null;
  ticket_url: string | null;
}
export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  text: string | null; // null = the turn is still running
  ts: string;
  failed: boolean;
  finished_at: string | null;
  agent: string | null;
  attachments: ChatAttachment[];
}
export interface ChatAttachment {
  name: string;
  size: number;
  type: string;
  image: boolean;
  url: string;
}
export interface ChatThread {
  conversation: ChatConversation;
  messages: ChatMessage[];
  busy: { since: string; log: string } | null;
}

/** 프로젝트 추가 wizard (/api/onboard/*) */
export interface GithubRepo {
  full_name: string;
  description: string | null;
  default_branch: string;
}
export interface PathCheck {
  exists: boolean;
  is_git: boolean;
  origin: string | null;
  matches: boolean;
}
export interface CloneJob {
  id: number;
  github: string;
  path: string;
  status: "running" | "done" | "error" | "exists";
  error: string | null;
}

/** GET /api/update (server.update_info); log = update.log lines after ?since= */
export interface UpdateInfo {
  current: string;
  latest: string | null;
  checked_at: string | null;
  blocked: string | null;
  /** Docker mode: the host command that updates instead of the button */
  command: string | null;
  notes: { tag: string; items: string[] }[];
  log?: string[];
}

/** GET /api/mode (server.mode_info, TP-165); log = mode.log lines after ?since= */
export interface ModeInfo {
  current: "docker" | "macos" | "linux";
  /** the last `tp mode` run (~/tp-work/mode-state.json) */
  state: { to: "docker" | "host"; done: string[]; started: string; finished?: string; port?: number; admins?: string } | null;
  /** Docker mode: the host command that switches back instead of the button */
  command: string | null;
  /** the Docker environment check; null in Docker mode */
  checks: { id: string; title: string; ok: boolean; detail: string; fix: string }[] | null;
  log?: string[];
}
