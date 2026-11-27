# toothpaste (치약)

Squeeze a ticket, get a PR.

The command is `tp` = **T**ooth**P**aste. (Inside, the package is still `ticket_pilot/` and the services, keychain items
and secrets keep their `ticket-pilot` names, so existing installs keep working.)

toothpaste runs a ticket → agent → PR → AI review → deploy pipeline on your own machine. It picks up tickets from
**Asana** (tasks tagged `tp`, setting `asana_tag`) and **GitHub issues**, gives each one a git worktree and a headless
coding agent (**Claude Code** `claude`, **Codex** `codex`, or **Antigravity** `agy`, your own login). The agent opens a draft PR,
another agent reviews it, and you approve merges and production deploys from the local dashboard.

- Runs on **macOS** and **Linux** (systemd), or in **Docker**.
- Needs Python 3.11+, `git`, `gh` (logged in) and the `claude`, `codex` or `agy` CLI (logged in). No pip packages.
- Everything stays local: the SQLite DB, the worktrees and the dashboard (`http://127.0.0.1:3032`).

```bash
gh repo clone bini59/toothpaste ~/tp-work/app
~/tp-work/app/bin/tp setup
```

<!-- TODO: screenshot of the dashboard (board + review) -->

Agents run without a sandbox, with your gh login: read [SECURITY.md](SECURITY.md) before sharing the dashboard.
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

## How it works

```
[tp tick: launchd com.ticket-pilot.tick runs `tp tick` every minute; each automation runs when its interval is due]

[automation "ticket-pilot: Asana dispatch", every 5 min]
  precheck: tp precheck-dispatch
    sync      Asana (tasks assigned to me, incomplete) -> SQLite
    dispatch  tagged + single-repo + no open dependency + free slot (max 3)
              -> git worktree add (<worktree_root>/<repo>/<ticket>, branch <branch_prefix><ticket>)
              -> copy the checkout's .env / .env.* files (same relative paths), then in the agent's wrapper:
                 pnpm > yarn > npm install by lockfile, the repo's setup script, then `codex exec` / `claude -p` / `agy`
                 with the brief, detached (ticket_pilot/runner.py; repo setting auto_setup: false turns the first two off)
    exit 0 only if a tagged ticket needs a repo choice (multi-repo project)
  agent: prompts/automation_dispatch.md  (picks a repo, tp assign, tp dispatch)

Agent in the worktree: implement -> commit -> push -> draft PR
  -> tp ws-set --status in-review                     (completion signal)
  -> or tp ws-set --status todo --comment "BLOCKED: ..."  (needs the owner)
  exits without either: exit 0 + open PR = in-review, else BLOCKED with its last message (pipeline.agent_exits)
  still running after agent_max_min (default 240): stopped, BLOCKED (event agent_timeout)

[automation "ticket-pilot: PR review", every 5 min]
  precheck: tp precheck-review
    scan  worktrees (git worktree list), agent exits, gh pr list per branch -> SQLite
    exit 0 only if an in-review PR has an unreviewed head commit
  agent: prompts/automation_review.md  (review, tp review-save -> DB + PR comment)

[automation "ticket-pilot: staging diff", every 10 min]
  precheck: tp precheck-diff
    diff-scan  per repo with a staging branch: <prod>...<staging> (GitHub, read-only), staging pipeline for the head,
               cherry-pick aware (git cherry in the local clone, objects only) -> staging_diffs -> 배포 page + sidebar count
    exit 0 only if a repo is ahead and its staging head has no AI note yet
  agent: prompts/automation_diff.md  (summarize what would ship + risks, tp diff-note --level ok|check|hold; read-only)

[automation "ticket-pilot: worktree cleanup", every 5 min]
  precheck: tp precheck-cleanup
    cleanup  ticket worktrees whose PRs are all merged: is every PR in what production runs now?
             (merge commit ancestry -> git cherry -> PR diff reverse-applies on the deployed tree)
             yes + safety checks pass -> git worktree remove + delete the local branch (remote branch/PR stay)
    exit 0 only if a PR failed those checks but production shows signs of it
  agent: prompts/automation_cleanup.md  (compares the code, tp cleanup-verdict --verdict live|not_live; never deletes itself)

Dashboard: http://127.0.0.1:3032 (setting dashboard_port; launchd agent com.ticket-pilot.dashboard: `bin/tp serve` on the host)
```

An automation's AI step is the same headless agent, started in this checkout; at most one runs per automation, and one
still running after 60 min is stopped. The dashboard's 로그 page shows the snapshot `tp tick` writes (meta.automations).
Agent logs, prompts and exit codes are in `data/agents/`. `tp attach KEY` prints the command that resumes a ticket's
agent session by hand (`codex resume <id>` / `claude --resume <id>` / `agy --conversation <id>`). Orca is not needed; runs Orca started before the
local runner keep reading their status from Orca until tp relaunches them, and cleanup removes their worktrees with
`orca worktree rm`.

Agents run with the owner's gh login and no sandbox, and the base branches are not protected on GitHub, so the brief's
"never merge / never push to main or staging" rules are checked after the fact on every scan (`pipeline.audit_pushes`):
a `[KEY] ...` commit of the ticket on main/master/staging/dev/develop/production or the repo's deploy branches while
its PR is unmerged, or a PR merged while tp's agent for it was running. Either stops the agent, blocks the ticket with
`BLOCKED: 규칙 위반: ...` (shown as 막힘 even over an approved review) and logs a `violation` event, once per commit/PR.
The owner's reply continues the ticket; 그만두기 drops it. Nothing is reverted automatically.

## Setup

```bash
gh repo clone bini59/toothpaste ~/tp-work/app
~/tp-work/app/bin/tp setup
```

Then finish on the **시작하기** page it opens in the browser (`http://127.0.0.1:<port>/#start`): Asana 연결 (PAT),
GitHub 연결 (Device Flow code), 사용자 이름, 첫 프로젝트 추가. A new install with no project opens it on the first visit;
later it is linked from 설정 (and ⌘K 시작하기).

Supported: **macOS** (the dashboard and the tick run as launchd agents) and **Linux with systemd** (systemd user units,
TP-162; tested on Ubuntu). Optional: **Docker** (below, TP-164). Not supported: Windows, WSL.

Needed first (`tp setup` checks all of them, lists everything missing with how to get it, and changes nothing until they are there):
- Python 3.11+ (`python3`). macOS: python.org Python 3 (https://www.python.org/downloads/macos/); Homebrew Python works
  but is warned about (see 고급 > Python)
- `git`, and `gh` logged in (`gh auth login`)
- `claude`, `codex` or `agy` CLI, logged in
- Linux only: `systemctl --user` working (a systemd user session: log in as that user over ssh or a desktop, not `su`;
  WSL and containers usually have none) and `lsof`. Linger is checked and only warned about (see 고급 > Linux)

`bin/tp setup` (`ticket_pilot/setup.py`, TP-144) is safe to run again; each step skips what is already done:
1. prechecks (above)
2. `web/dist`: when missing, the current tag's release asset `web-dist.tar.gz` (same download as `tp update`), else `npm ci && npm run build`
3. port: the setting `dashboard_port` when stored; otherwise 3032, or the next free port when 3032 is taken (another
   user's dashboard on the same Mac), saved to `dashboard_port`. An install from before the setting keeps 3032
4. `launchd/install.sh` (skipped when this checkout's dashboard already answers), which waits for `/api/state`. On
   Linux it hands over to `systemd/install.sh` (고급 > Linux); every caller (`tp update`, the deploy workflow, 로컬망 공유)
   runs `launchd/install.sh` on both systems
5. the browser on `http://127.0.0.1:<port>/#start` (`open` on macOS, `xdg-open` on a Linux desktop); with neither (a
   server) it only prints the address

Everything toothpaste keeps on a Mac goes under `~/tp-work` (no leading dot: dev servers that serve static files refuse
paths with a dot folder):

```
~/tp-work/
  app/         this checkout (with data/)
  worktrees/   <repo>/<ticket slug>            (setting worktree_root)
  repos/       repos the 프로젝트 추가 wizard clones  (setting clone_root)
  logs/        launchd / systemd logs (dashboard.log, tick.log)
```

### Docker로 실행하기 (optional, TP-164)

The default stays the host install above. `docker/Dockerfile` (Debian slim, Node LTS, python3, git, gh, pnpm / yarn via
corepack, `claude`, `codex`; `web/dist` built in the image; runs as the non-root user `node`; no secrets in the image) and
`docker/compose.yaml` run the dashboard and a tick every minute in one process, `tp run`. On a Mac or a Linux host
with Docker Compose and Python 3.11+, the installer starts the dashboard and registers the update manager together:

```bash
gh repo clone bini59/toothpaste tp && cd tp
cp docker/.env.example docker/.env      # set TP_ADMINS=<your GitHub login>[,<more>] and/or TP_ORG=<org>
python3 -m ticket_pilot.docker_update_manager install
(cd docker && docker compose exec tp git config --global user.name "<name>") # and user.email, for agent commits
```

Open `http://127.0.0.1:3032` and sign in with GitHub. `TP_PORT` in `docker/.env` changes the host port.

Then 시작하기 as above. Its 에이전트 로그인 step (the same card as 설정 → 에이전트 → 활성 계정) logs `codex` in with
Device Flow and `claude` with `claude setup-token` (or a pasted token / API key) from the browser, no terminal needed.

- **Login for everyone.** The image sets `TP_MODE=docker`: every request needs a GitHub login (the 로컬망 공유 Device Flow),
  127.0.0.1 included. Allowed: the logins in `TP_ADMINS` (comma separated) and, when `TP_ORG` is set, that org's members.
  With both empty `tp run` prints why and exits (compose keeps restarting it: read `cd docker && docker compose logs tp`). What only 127.0.0.1 may do on a host install (force logout, ...) only
  `TP_ADMINS` may do. Writes need Origin == Host, and every action is recorded under the GitHub login.
- **Port** `127.0.0.1:${TP_PORT:-3032}`: this machine only.
- **Volumes**: `tp-data` (`data/`: `tp.db`, logs, `secrets.json` for the Asana / GitHub tokens, tp's gh config),
  `tp-home` (`~`: `~/.claude`, `~/.codex`, `~/.config`, `~/.gitconfig`), `tp-work` (`~/tp-work/worktrees`, `~/tp-work/repos`).
  The DB lives only in the named volume and only processes in the container write it; never share a `tp.db` between a
  host process and a container over a bind mount (TP-8).
- **`tp run`**: a tick every minute like launchd / systemd (skipped while the last one still runs; agents in their own
  sessions). `docker compose stop` (SIGTERM) waits for the running tick (`stop_grace_period: 5m`).
- **Off in Docker mode**: `tp setup`, `tp migrate`, `tp update` and 로컬망 공유 ("Docker 모드에서는 쓰지 않아요"), 시스템 자동화.
- **Update from the dashboard**: users in `TP_ADMINS` can use **설정 → 고급 → 업데이트** after installation.
  For an existing Docker deployment without the manager, wait for agents and dev servers to finish, then run
  `git pull --ff-only && python3 -m ticket_pilot.docker_update_manager install` once from the host install checkout.
  The host manager downloads the published GHCR image and replaces only `tp`. It refuses to interrupt active work.
  For manual maintenance of a managed install, run Compose with `-f compose.yaml`, the existing `-f compose.override.yaml`
  if present, and `-f compose.update.yaml` from `docker/`. Use a host-only install checkout when migrating bind mounts.
  The first GHCR publication creates a package that the repository owner must make public for anonymous downloads.
- **Limits**: manually stopping or rebuilding the container ends the agents running in it. Repos whose agents need Docker do not work (no Docker socket in the container); Docker Desktop on a Mac has
  slow file I/O in volumes, so installs and builds in worktrees are slower than on the host.

#### 외부 공개: Cloudflare Tunnel (optional, TP-166)

Docker mode only (every request logs in, so a tunneled request cannot skip the login; a host install trusts 127.0.0.1
without one and is never exposed this way). The compose profile `tunnel` adds a `cloudflared` container; the host port
stays on 127.0.0.1.

1. Cloudflare Zero Trust → Networks → Tunnels → create a tunnel (cloudflared) → copy its token into `docker/.env` as `TUNNEL_TOKEN`.
2. Public hostname: `tp.example.com` → service `http://tp:3032` (the compose network name of the dashboard).
3. Set `TP_PUBLIC_URL=https://tp.example.com` in `docker/.env`. From `docker/`, run
   `docker compose -f compose.yaml -f compose.update.yaml --profile tunnel up -d`. Include `-f compose.override.yaml`
   before `-f compose.update.yaml` if the installation has a Compose override.
4. **Strongly recommended**: a Cloudflare Access application on that hostname (company email domain or your IdP) as a
   second lock in front of the GitHub login.
5. **Risk**: whoever logs in can run commands inside the container through the chat agents (and use every token in it).

With `TP_PUBLIC_URL` (an `https://` origin, no path; anything else stops `tp run`) the login cookie gets `Secure`, a
write's Origin must be that address or `http://127.0.0.1:<port>`, and the login screen points visitors at it. The
sessions list and the login event record `Cf-Connecting-IP`; that header is never used to decide access.
설정 → 고급 → **외부 공개** (TP_ADMINS only) shows whether cloudflared is up and connected (its `:2000/ready` on the
compose network), the public address and the Access advice; the token and domain are never entered in the dashboard.

### 실행 방식 바꾸기: host <-> Docker (TP-165)

An existing host install switches to Docker and back with one command (or 설정 → 실행 방식, which runs the same
command detached and shows its log). Like `tp migrate`: a preview by default, `--apply` to do it, the same command again
resumes after a failure.

```bash
bin/tp mode                      # the Docker environment check: docker, the daemon, compose v2, disk, CPU, Docker Desktop file sharing
bin/tp mode docker [--admins a,b]          # preview: mounts, admins, tokens, warnings; changes nothing
bin/tp mode docker --apply [--admins a,b]
bin/tp mode host [--apply]                 # back (run on the host; the container has no docker)
```

- **Host -> Docker**: refuses while an agent, automation, chat, dev server or tp check runs (`migrate.busy`) or a check
  fails; holds the tick lock; stops launchd / systemd; backs up `tp.db` to `data/backups/`; writes
  `docker/compose.override.yaml` (gitignored) that mounts every `repos.path`, `worktree_root`, `clone_root`, `~/.claude`,
  `~/.codex` (and `~/.gitconfig` read-only) **at the same absolute paths**, builds the image with the host uid/gid and
  `HOME` = the host home, so no DB path changes and `claude --resume` keeps working; `docker/.env` gets `TP_ADMINS`
  (`--admins`, default: the connected GitHub account) and `TP_PORT` (`dashboard_port`); copies the DB into the `tp-data`
  volume and the tokens (`core.secret_get`, over stdin, never printed) into its `secrets.json`; `docker compose up -d`
  and waits until `/api/state` answers 401; then the host `tp.db` is chmod 444, a read-only backup (TP-8).
- **Warnings** in the preview (shown, never fixed): hook commands in `~/.claude/settings.json`, `~/.codex/config.toml`
  values with localhost or Mac paths, and on macOS that the container only sees `~/.claude/.credentials.json`, not the
  keychain login (log `claude` in on 설정 → 에이전트 → 활성 계정 if it fails there).
- **Docker -> host**: `docker compose stop` (waits for the tick), backs up the host DB, copies the volume's DB and tokens
  back, `docker compose down` (volumes stay; `down -v` removes them), `launchd/install.sh` (only if the services were
  installed before).
- Progress: `~/tp-work/mode-state.json`; a failure prints the finished steps and how to undo them. The dashboard's run
  logs to `~/tp-work/logs/mode.log`. 설정 → 실행 방식 is 127.0.0.1 only on the host; in Docker it shows the host command.
- While switched to Docker, `tp update` / `tp setup` / `tp migrate` on the host refuse (so a deploy of `main` fails
  instead of starting a second tick through launchd). The host Docker update manager described above handles dashboard updates.

### 고급

**Python and local network permission.** Install python.org Python (https://www.python.org/downloads/macos/), run
its `Install Certificates.command` (in `/Applications/Python 3.x/`), and allow Python under System Settings > Privacy & Security >
Local Network (accept the prompt when it appears); if the checkout is under `~/Desktop`, also give
`/Library/Frameworks/Python.framework/Versions/Current/Resources/Python.app` Full Disk Access. `install.sh` uses `/Library/Frameworks/Python.framework/Versions/Current/bin/python3`
when it exists and falls back to `/opt/homebrew/bin/python3`. Agents started by `tp tick` use the tick job's local network
permission; Homebrew python is ad-hoc signed, so every upgrade loses it and LAN connections fail with `No route to host`.

**Linux** (TP-162). `systemd/install.sh` (run through `launchd/install.sh`) fills `systemd/ticket-pilot-dashboard.service`,
`ticket-pilot-tick.service` and `ticket-pilot-tick.timer` (every minute) with this checkout's path, `python3` from the
PATH, `dashboard_port`, the 로컬망 공유 host and `TP_LOG_DIR` (default `~/tp-work/logs`, `dashboard.log` / `tick.log`; also in
`journalctl --user -u ticket-pilot-dashboard`), installs them to `~/.config/systemd/user/`, runs `systemctl --user
daemon-reload`, `enable --now` the timer, restarts the dashboard and waits for `/api/state`. Restart by hand:
`systemctl --user restart ticket-pilot-dashboard`. User units stop when you log out unless linger is on:
`sudo loginctl enable-linger $USER` (tp never runs it; `tp setup` and `install.sh` only warn). Tokens (Asana PAT, GitHub)
go to `~/.config/ticket-pilot/secrets.json` (mode 600 in a 700 folder; tp refuses to read it when the mode is wider)
instead of the keychain; `ASANA_PAT` still wins. `tp migrate` and the launchd list on 시스템 자동화 are macOS only.

**By hand instead of `tp setup`.** The UI without Node:
`mkdir -p web/dist && gh release download "$(git describe --tags --abbrev=0)" -p web-dist.tar.gz -O - | tar -xz -C web/dist`
(`bin/tp update` keeps it current). Only people changing the UI build it: `cd web && npm ci && npm run build` (see Web UI).
Then `launchd/install.sh` -> `http://127.0.0.1:<dashboard_port>` (localhost only).
The script fills `launchd/com.ticket-pilot.{dashboard,tick}.plist` with this checkout's path, installs them to
`~/Library/LaunchAgents` (logs in `~/tp-work/logs/{dashboard,tick}.log`; another folder: `TP_LOG_DIR=<absolute path> launchd/install.sh`
on every run, repository variable `TP_LOG_DIR` for the deploy workflow), restarts them and waits for `/api/state`.
It first disables any enabled Orca automation whose precheck is this checkout's `bin/tp`, so the two never dispatch twice.
Agents run as `codex exec --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust` or
`claude -p --dangerously-skip-permissions` (settings `agent`), with the user's own CLI login and config.
Python changes need a restart (`launchctl kickstart -k gui/$(id -u)/com.ticket-pilot.dashboard`),
UI changes a `npm run build`; `config/` is read live. The deploy workflow does both on every push to main.
A host process and a container never share one `tp.db` over a bind mount: SQLite file locks do not cross a Docker
Desktop bind mount (two writers corrupted the DB on 2026-10-20, TP-8). Docker mode keeps its DB in a named volume that
only the container writes.

**Tokens from a terminal.** Asana PAT (Asana > My apps > Personal access tokens), instead of 설정 > Asana 연결:
`security add-generic-password -U -s ticket-pilot-asana -a asana -w`  (prompts for the token; on Linux set `ASANA_PAT` or
use 설정), then `bin/tp sync && bin/tp scan`.
GitHub 연결 (and the 로컬망 공유 / Docker login) uses GitHub's Device Flow on the shared public OAuth App "Ticket-pilot"
(its client id is built in, no secret). To use your own app instead: create an OAuth App on GitHub, check
**Enable Device Flow**, and put its client id in `data/github-oauth.json` (`{"client_id": "<id>"}`) or in the keychain:
`security add-generic-password -U -s ticket-pilot-github-oauth -a client_id -w <id>`.

An install from before TP-121 keeps its stored `worktree_root` / `clone_root` (`~/ticket-pilot/worktrees`,
`~/Desktop/07_Project`) and its checkout path until it is moved with `tp migrate`:

**Moving an existing install** (TP-122). Moving the folders by hand breaks it: absolute paths are in the DB, in git's
worktree links, in the launchd plists and in the names of Claude's session folders. In this order:

1. `bin/tp migrate --to ~/tp-work`: preview. Prints what moves where, how many DB values change per column and what
   happens to launchd; changes nothing.
2. `bin/tp migrate --to ~/tp-work --apply`, from a terminal, when nothing is running. It refuses (nothing changed) while
   a ticket / automation / chat agent or a dev server runs, or when `<to>/app` or `<to>/worktrees` already exists; a
   source on another disk is copied instead of renamed, and it asks before doing that. Then: waits for the running tick
   and stops the two launchd agents (only when their plists point at this checkout) -> `data/backups/tp-<time>.db` ->
   moves `worktree_root` to `<to>/worktrees` and the checkout (with `data/`) to `<to>/app` -> `git worktree repair` in
   every repository that has worktrees there (toothpaste's own moves on both ends) -> replaces the old paths in every
   text value of the DB and sets `worktree_root` / `clone_root` (`<to>/repos`) -> copies the Claude session folders
   (`~/.claude/projects/<path>`) of the checkout and the worktrees to their new names, so `claude --resume` still finds
   chat and ticket sessions -> `launchd/install.sh` from `<to>/app` (logs: `TP_LOG_DIR`, else `<to>/logs`).
   Repositories themselves (`repos.path`, the owner's own clones) are never moved, only toothpaste's checkout.
3. Set the repository variable `TP_DIR` to `<to>/app` (`gh variable set TP_DIR --body ...`); until then the next deploy
   looks for the checkout at the old path and fails. The command ends with this and the other leftovers (old log
   folder, Python virtualenvs inside worktrees, an optional symlink from the old checkout path for agents that remember
   the old `bin/tp`).

On a failure it prints the finished steps and how to go back by hand (restore the DB backup, `mv` the folders back,
`git worktree repair`, `launchd/install.sh`). Progress is kept in `<to>/migrate-state.json`: the same command run again
skips what is done (once the checkout has moved, run it as `<to>/app/bin/tp migrate ...`).

### Web UI (React)

`web/` is a Vite + React + TypeScript + Tailwind v4 app. The Python server serves the built `web/dist` and the `/api/*` JSON.

- Only needed to change the UI (installs and `tp update` use the built `web-dist.tar.gz` of the release): Node/npm.
  Every package comes from the public npm registry.
- Live UI work: `cd web && npm run dev` → http://localhost:5173 (proxies `/api` to the server on :3032).
- Base components (Sidebar, Header, Card, Badge, Button, Input, `cn`) are in `web/src/ui/`; their base styles sit in the
  components layer (`web/src/ui/ui.css`), so a utility passed in `className` overrides them. `cn` only joins class names.
- Colors and the font are Tailwind `@theme` tokens in `web/src/theme/tokens.css` (fixed 50-900 scales plus aliases such as
  `bg-surface`, `text-text-muted`, `border-border`). The 작업실 canvas reads them as CSS variables at runtime, so keep them hex.
- App-level components (Select, Table, ...) are in `web/src/components/`.

#### Themes (`ticket_pilot/themes.py`, TP-157)

A theme is one JSON file that overrides those tokens, like Claude Code's `~/.claude/themes/*.json`:

```json
{"name": "Corp", "base": "dark", "tokens": {"color-primary": "#2f6fde", "font-sans": "\"Pretendard\", sans-serif"}}
```

- Built-in: `web/themes/light.json` (= the `tokens.css` defaults) and `dark.json`. User themes: `../themes/*.json` next to the
  checkout (`~/tp-work/themes`; not a setting, and never in the repo). The file name without `.json` is the theme id; a
  user file named like a built-in (`light`, `dark`, `auto`) is ignored.
- `tokens` keys are the `tokens.css` variable names (`color-primary` or `--color-primary`). A missing token takes the
  value of `base`'s built-in theme. Colors must be `#rrggbb` (the 작업실 canvas parses hex); `font-sans` takes a font list.
  Anything else (unknown names, `;`, `}`, `url(`, …) is dropped with a warning, so a theme cannot inject CSS.
- `GET /api/themes` lists them (id, file, name, base, resolved tokens, warnings) with the selected id. The choice is the
  global setting `theme` (default `light`; `auto` follows `prefers-color-scheme`), picked on 설정 → 테마. A missing or broken
  selected theme falls back to `light`.

## Deploy rules and the 배포 page

Per-repo rules live in the settings, `repo.<name>.deploy` (설정 page → 배포; 2026-10-19):

| Repo | Branches (test → prod) | Prod verified by | Modes |
|---|---|---|---|
| `01_Web` | `staging` → `main` (+ weekday train) | prod CodePipeline (+ ECS, smoke) | staging, direct, promote, all |

More shapes (a `dev` staging branch with `pr_into_staging`, a GitHub Actions workflow as the prod check, `version: npm-patch`
with `via: main`, direct-only repos) are in `config/settings.example.json` (`deploy.repos`).

Ticket base branch (`core.feature_base`): train repos and repos with `pr_into_staging: true` (`02_Api` in the example) branch ticket worktrees
from `origin/<staging>` and open PRs with `--base <staging>`; an open PR on the prod target created after the repo's
`retarget_since` (else `release.retarget_since`) is retargeted to staging on scan. Other repos use the GitHub default branch.

Modes:

1. **staging** (티켓 → staging): the AI-approved PR is merged into the repo's staging branch (the approval below). A staging branch
   can be `dev`; the `preview_label` (single preview slot) is only used for a PR of such a repo that is not based on `dev`.
2. **direct** (티켓 → prod, staging 건너뜀): PR base already the target branch → merge it (repo merge method). Otherwise cut
   `release/<key>-<stamp>` from the target, cherry-pick the PR's own non-merge commits (`-x`), merge that, then merge the
   original PR into its own base too (so staging/dev do not drift).
3. **promote** (staging의 티켓 → prod): a PR already in staging → same release-branch cherry-pick onto the target.
4. **all** (staging 전체 → prod): staging pipeline must be green, then `staging -> <prod>` PR (`via: main` repos: release branch from main
   with staging merged in + bump). Everything in staging ships, including commits made outside toothpaste.

A repo with `via: main` and `version: npm-patch`: the release branch also bumps the package.json patch version; after it lands in
`main`, `main -> production` is merged only if main holds nothing else, then tag `vX.Y.Z` + GitHub release on the main merge
commit. Git work runs in a throwaway `git clone --shared` of the local checkout (`/tmp/tp-release-*`), pushed with `--no-verify`.

Flow: the button only inserts a `releases` row (`mode` set, status `requested`). Every precheck (`tp deploys`, inside
`_release_steps`) runs it immediately and resumably: `requested → running (merge: waits for checks/mergeability up to
deploy_timeout_min; `via: main` promote) → deploying (prod pipeline/workflow, ECS, smoke) → done | failed`. Done marks the PRs
released and completes the Asana tasks, so tickets waiting on them (`선행 작업 대기`) unblock on the next sync. One deploy per
repo runs at a time (requests queue in order; the train waits for running dashboard deploys). Nothing is rolled back.

CLI: `tp deploy {staging|direct|promote|all|preview} [--ticket KEY] [--repo NAME]`, `tp deploy-cancel ID`, `tp deploys`.

The 배포 page also shows **staging에 있고 프로덕션에 없는 것** per repo (from the staging-diff automation): commits/tickets/PRs
that would ship, those already live through a cherry-pick (struck through), prod-only commits that staging lacks (conflict
risk), the staging pipeline, the verdict (배포 가능 / staging 빌드 중 / staging 빌드 실패) and the AI note
(배포 추천 / 확인 필요 / 보류). It only flags; deploying is still a button press.
CLI: `tp diff-scan`, `tp diff-queue --json`, `tp diff-note --repo R --head SHA --level ok|check|hold --file note.md`.

## Staging approval and the weekday release train

1. AI review says 괜찮음 → the card in 리뷰 완료 shows **<staging> 머지 승인** for any repo whose rule has a staging branch and
   the `staging` mode (PR base must be that branch).
2. Pressing it only records the request. Within 5 min a host precheck
   (`tp approvals`, run by both automations) re-checks the head/base/conflicts, marks the draft ready and merges it into
   staging with a merge commit (tp's GitHub token, no GITHUB_TOKEN) → stage `staged` (배포 대기).
3. Train repos (`deploy.repos.*.train`, in settings order) — weekdays 10:00-12:00 KST (`release`), the first precheck
   tick starts the train (`tp release-tick`): per repo, abort if `main...staging` holds any commit that is not
   from a staged PR or the staging pipeline is not green; else open + merge `staging -> main`, then later ticks wait for
   the prod CodePipeline, the ECS rollout and smoke URLs. Success marks the PRs released and completes the Asana tasks.
   Any abort/failure stops the rest of that day; nothing is rolled back automatically. A missed window waits for the next weekday.
   Other repos' staged tickets go to production from the 배포 page (promote / all).
4. 배포 tab: deploy buttons per ticket and per repo, live deploys, the train (next time, contents, hold switch), history.

CLI: `tp approve KEY [--cancel]`, `tp approvals`, `tp release-tick [--force]`, `tp release-status`.

## Milestones (`ticket_pilot/milestone.py`, 2026-10-30)

Use `tp milestone create --project <id> --title "Release" --subtask "First change"` to ship a group of tickets together.
The parent is an Asana task or GitHub issue marked with `milestone_tag` (default `tp-milestone`), or a page in the configured Notion milestone database. The repo needs a staging merge route. See `tp guide milestone` for the per-source settings.

1. The first dispatched subtask (tagged with `asana_tag` as usual) creates `milestone/<parent gid>` from staging in its repo and a
   milestone ticket `ms:<parent gid>:<repo>` (stage `collecting`, 하위작업 진행 중). Subtask worktrees branch from the milestone
   branch and their PRs target it; open subtask PRs on staging are retargeted.
2. A subtask PR the AI review approves (and tp checks pass) is merged into the milestone branch on the next tick without
   the dashboard button (`tp milestones`, also part of every tick, requests it; `tp approvals` merges it; once per head)
   → stage `milestone` (마일스톤에 합침). While subtasks are open, staging is merged into the milestone branch so it does not drift.
3. When every tagged subtask is in the milestone branch (or done / closed / skipped), the milestone ticket waits in 대기
   (`ms_hold`, 작업 완료 대기) until the owner presses **마일스톤 작업 완료** on its 마일스톤 page (`tp milestone-done <parent gid>`;
   refused while a subtask is in progress, and cleared again by the next tick when one is in progress again). Then the
   host checks the branch out in a worktree and opens `milestone/<parent gid> -> staging`. The milestone ticket then runs like any ticket: AI review,
   rework / conflict agents on that branch, the owner's staging approval, the weekday train (which accepts the milestone's
   commits, not commits staging got elsewhere).
4. Live in production: the milestone ticket, the subtasks merged into it and, once every repo of the milestone is live,
   the source parent are completed.
5. Subtasks added later: while the milestone PR is open, a subtask still in progress puts the milestone ticket back in
   `collecting` (no AI review, no staging approval) until it is in the branch. A subtask merged after the milestone PR
   starts the next round: right away when that PR is only on staging (2026-11-10; the train holds its staging commits
   until the next milestone PR is merged), or once it went live
   (tick reopens the ticket). The next milestone PR ships them; each round completes only the subtasks it carried.

**dev 서버** (`ticket_pilot/devserver.py`, 2026-11-04): the milestone page has a dev server panel per repo to try
the subtasks together on `milestone/<parent gid>`. 띄우기 first checks: it checks the branch out where the milestone PR's
worktree goes (or fast-forwards it), copies missing .env files from the main checkout, compares each `.env.example` with
the real .env (missing or empty keys are warned about, values never shown), and picks the command and port (repo setting
`dev` = `{cmd, port}`, `{port}` in cmd; else package.json `dev` / `start:dev` / `start` with `--port` for next and vite,
`PORT` for everything; port from the script, .env `PORT`, vite.config, next 3000 / vite 5173). A port someone already
listens on is shown with its processes (pid, command, cwd); the owner chooses to kill them and start there, or start on
another port (the next free one is offered). Servers run detached (log in `data/dev/`) and survive dashboard restarts;
their state is `meta.dev_servers`.

## Production tracking for everything else (`ticket_pilot/track.py`)

A merge is not a deploy. A merged PR that toothpaste did not ship itself gets stage `merged` (or `staged` when it sits in
a staging branch) and sits in **배포 대기** until it is seen in production; then it moves to 완료 and the Asana task is
completed. Every review precheck (`tp track`) reads what production last deployed per repo (`deploy.repos.*.prod`) and
checks whether the PR's merge commit is an ancestor of it, or, for cherry-pick releases, whether every own commit has a
patch-equivalent there (`git cherry` in the local clone, after fetching objects only). Nothing is merged or deployed by tracking.

## Worktree cleanup (`ticket_pilot/cleanup.py`)

2026-10-19: once a ticket's change is in production, its worktree goes away right away. Only ticket worktrees
(`runs.worktree_path`); manually created worktrees (git or Orca) are never touched. Production is the criterion, so a merge into
staging/dev is not enough, and cherry-pick releases count. For each merged PR of the worktree (no open PR allowed), against
the sha production runs now (`track.deployed_sha`):

1. `merge`: the merge/squash commit is an ancestor of it.
2. `cherry`: every own non-merge commit has a patch-equivalent there (`git cherry`).
3. `patch`: the PR's whole diff (merge-base..head) reverse-applies cleanly onto the deployed tree (conflict-fixed cherry-picks, squashes).
4. `agent`: if 1-3 fail but production mentions the PR/key/commit shas or changed its files after the merge, the cleanup
   automation's agent compares the code and records `live` / `not_live` for that deployed sha (`tp cleanup-verdict`).

Safety checks keep the worktree (reason in `cleanups.reason` + a `cleanup` event in 로그): uncommitted changes, local commits in
neither the PR heads nor production, an open PR from the branch, a ticket agent still running (or an Orca agent not in
state `done` for worktrees Orca made), or a process listening on a port with its cwd in the worktree (dev server; idle
agent CLIs are ignored). Removal is `git worktree remove` (no --force; `orca worktree rm` for worktrees Orca made, verified
with `git worktree list` + the path), then `git branch -D` of the local branch. The run gets `removed_at`.
Results are cached per deployed sha + PR heads, so a worktree is re-compared only after a new production deploy.

Each scan then prunes Docker (2026-10-22; agent test builds filled the disk): build cache and images older than 24h
that no container (running or stopped) uses. Containers and volumes are never touched; reclaimed space is logged as a
`cleanup` event.

CLI: `tp cleanup [--dry-run]`, `tp cleanup-queue --json`, `tp cleanup-verdict --path P --prod-sha S --verdict live|not_live --note "..."`.

## Using it

### GitHub issues and toothpaste's own deploys

The `github_issues` setting maps a repo to a ticket key prefix (`{"32_Ticket_pilot": "TP"}`). Every open
issue of that repo is synced as a tagged ticket for it (key `TP-<n>`, gid `gh:<owner/repo>#<n>`) and dispatched like an
Asana ticket; a closed issue completes the ticket. Repos of a project with `source: github` (TP-92) only sync open
issues labeled `tp` (key prefix from `github_issues`, else from the repo name); removing the label drops the ticket like an Asana task
reassigned away, adding it back brings it back. The issue body and comments go into the work brief. Once the change is
live, toothpaste comments on the issue and closes it (instead of the Asana completion).

toothpaste itself (this repository, 2026-10-20): `main` is production. Its deploy rule sets
`staging.branch: main`, so **main 머지 승인** on the dashboard merges the reviewed PR into main. The push runs
`.github/workflows/deploy.yml` on this Mac's self-hosted runner (label `tp-mac`, `~/actions-runners/32_Ticket_pilot`,
launchd service): `bin/tp update --to "$GITHUB_SHA"` in the host checkout (repository variable `TP_DIR`, required: the
workflow stops without it; it must stay on `main`), the same command colleagues use (next section). track.py reads the latest green
run of that workflow, so the ticket moves to 완료 and its issue is closed.

The public repository `bini59/toothpaste` (TP-186) has no self-hosted runner (a fork PR must not run code on the owner's
Mac): its `.github/workflows/release.yml` only tags releases on GitHub's runner, and every install, the owner's
included, updates from those releases (`tp update`, `update.auto`). Its deploy rule uses `prod.release: true` instead of
`prod.workflow`: a merged PR is live once the latest GitHub Release's tag contains its commit. Both workflows check
`github.repository`, so during the switch each repository releases a commit once. `scripts/export-public.sh <empty
folder>` exports HEAD for the public repository (`git archive`; `.gitattributes` leaves `deploy.yml` out) and runs
`scripts/oss-check.sh` on the result.

### Versions and `tp update` (`ticket_pilot/update.py`, TP-140)

A version is a git tag `vX.Y.Z` with a GitHub Release (`web/package.json`'s `version` is unused). The full Actions suite
(OSS, Linux and macOS unit and web checks, Docker, and mode switching) runs when a same-repository `staging → main` PR
opens, reopens, receives a new commit, or is retargeted to `main`. Title and description edits leave the test run alone.
`deployment-tests-passed` succeeds only for an eligible PR after every job in the suite passes. The lightweight
architecture map check runs on all PRs, including milestone subtasks.

Keep the first deployment PR in draft until protection is set up. Once `deployment-tests-passed` succeeds, add a branch
protection rule for `main` in GitHub **Settings → Branches**. Require a pull request and `deployment-tests-passed` from
**GitHub Actions**. Use loose status checks so `staging` can keep its own history between releases. Enable **Do not
allow bypassing the above settings**, leave the bypass list empty, and allow merge commits. The dashboard and release
train wait for this protection and a test of the current merge candidate, then call the GitHub PR merge API with the
tested `staging` SHA. GitHub checks that the PR can still merge and applies the protection to its current base. The host
confirms the PR merged into `main` and that the actual commit has the tested file tree. Keep the PR target unchanged
while a deployment is in progress because the merge API pins only the head.

When GitHub marks the PR merged, `.github/workflows/release.yml` independently verifies that the latest `test` run for
that PR's final head passed before the merge and that its tested file tree matches the commit on `main`.
It uses the actual `main` merge commit to tag, publish the GitHub Release and its `web-dist.tar.gz` asset, and publish
the Docker image. A `main` push alone triggers neither the full tests nor an automatic release. If `main` changes the
deployment PR's merge candidate, update `staging` to produce a new test result. To publish other `main` changes manually,
select `main` in the release workflow's **Run workflow** menu and optionally enter a version.

The private repository's `deploy.yml` release job tags after its deploy. Put a `minor` or `major` label on the PR
before merging for that version bump. The default is a patch bump, and a commit that already has a version tag is skipped.

To update another Mac's checkout (on `main`, no modified tracked files): `bin/tp update` (or `--to <tag|sha>`). It
fetches the tags, fast-forwards to the latest release tag and, when `web/` changed, replaces `web/dist` with the
release's `web-dist.tar.gz` (`gh release download`; unpacked in `data/` first, so a broken download keeps the old UI),
then `launchd/install.sh`, and prints old -> new version with the release notes. No Node needed. Without
that file (older releases, `--to <sha>`) or with `--build`, it runs `npm ci` (when `web/package-lock.json` changed) and
`npm run build` instead; the deploy workflow targets a sha, so it always builds. The release job attaches the file
after each deploy, from the host checkout's `web/dist` once its HEAD is the tagged commit.
A run that stopped after the fast-forward (build or restart failed) finishes when run again. Log:
`$TP_LOG_DIR/update.log` (default `~/tp-work/logs`). The dashboard shows the version under the title; tp tick fetches the
tags every `update.check_min` minutes (default 360, at least 10) and, when a newer release exists, the sidebar shows **업데이트 vX.Y.Z**: its page lists the release
notes and has the 업데이트 button, which runs `tp update` detached and waits for the dashboard to come back.
With `update.auto` on (설정 → 고급 → 업데이트, default off) the tick runs that same `tp update` itself once per new
release; when it cannot run (`blocked`: not on `main`, modified files; Docker mode) it skips and leaves a 수신함 alert.
To check now instead of waiting for the interval, press **지금 확인** in 설정 → 업데이트 (or on the 업데이트 page):
`POST /api/update/check` fetches the tags right away, updates the last check time and returns the versions and notes
(a failed fetch returns its reason; concurrent clicks share one fetch). The automatic check is unchanged.
Downgrades are not supported.

- In Asana, add the ticket tag (setting `asana_tag`, default **tp**) to a task assigned to you. Within 5 min it is dispatched (or shows up as
  "레포 선택 필요" / "매핑 없음" on the dashboard, where you pick the repo).
- Ticket number = Asana `ID` custom field (e.g. `CRSC-340`), or `AS-<gid>` when the project has none.
  Worktree / branch name = lowercased ticket number (`<worktree_root>/<repo>/crsc-340`, branch `tp/crsc-340`).
- Relationship map Asana project ↔ repo, repos, deploy rules and global values: the settings in `data/tp.db` (next section).

## 프로젝트 page: chat per repo (`ticket_pilot/chat.py`)

프로젝트 lists the repos in the settings; each has a 채팅 tab and a 설정 tab. A conversation is a series of headless
agent turns in the repo's main checkout, with the repo's agent settings (`repo.<name>.agent`, else the global agent).
The first turn sends `prompts/chat.md` (read-only checkout, experiments in a /tmp clone, ticket creation through
`tp ticket create` when the owner asks) with the message; later turns resume the recorded session (`codex exec resume <id>` /
`claude --resume <id>`) with the new message and a reminder to use `tp chat attach <path>...` for reply attachments.
Changing the repo's agent tool starts a new session. Rows live in
`chat_conversations` / `chat_messages`; an assistant row with `text` NULL is the running turn (one per conversation).
Reading the conversation finalizes finished turns from the runner log: stdout is the reply (codex `-o` file first),
stderr goes to `<log>.err` and is shown as the progress log while the turn runs.

## Settings (`data/tp.db`, 설정 page, `tp config`; TP-49)

Settings are host state (checkout paths, deploy rules), so they live in SQLite, not in git:
`config_global` (top-level values, incl. `worktree_root` and `branch_prefix`), `config_repos` (per repo: path / GitHub /
description / `agent` (`{tool, model, effort, ultracode}` for this repo's ticket agents: codex or claude, model name or
alias, reasoning effort, Claude's ultracode toggle passed as `--settings '{"ultracode": true}'`; empty values = the
global `agent` tool and the CLI's own config; the 설정 page's 에이전트 section) / `setup` (script run in each new ticket worktree before the agent: install deps, copy `.env` from
`$TP_ROOT_PATH`; `$ORCA_ROOT_PATH` / `$ORCA_WORKTREE_PATH` work too) / `worktree_base` (start branch when the repo has no
feature base), `checks`, `ci`,
`review` overrides, deploy rule), `config_projects` + `config_project_repos` (Asana project ↔ repo links, role,
default), `config_hidden` (hidden Asana projects) and `settings_history`. `core.settings()`, `core.repo_rules()`,
`core.project_map()` and `checks.repo_checks()` build the same dicts the old JSON files gave. Defaults are in code
(`core.DEFAULT_SETTINGS`: `max_active_runs` 3, `review.max_rework_rounds` 3, `check.timeout_min` 10,
`check.max_parallel` 2, ...); a key the DB does not have uses them. The `repos` / `project_map` tables are derived
copies (`load_map`, on every sync and every settings change) that the queries join on.

- **First start**: a DB without settings imports `config/settings.json` + `config/project_map.json` once (event
  `config`, meta `config_imported`); after that the files are never read. Git only has the format samples
  `config/settings.example.json` and `config/project_map.example.json`; the real files are ignored.
- **Change**: on the dashboard, global values on the 설정 page and each repo's values on 프로젝트 → <repo> → 설정
  (기본, 에이전트, Asana 연결, 검사, 리뷰, 배포, 이력), or the CLI.
  Every change is validated (nothing is saved on an error, the reason is shown), recorded in `settings_history`
  (time, dashboard / cli / import, scope, key, old, new) and exported to `data/backups/config-<time>.json` (newest 50
  kept; the DB was damaged once, 2026-10-20). New values apply from the next tick; running work is not touched. Values
  that need a restart (`dashboard_port`, Asana workspace / keychain service) are read-only on the page.
- **Secrets** never go into the settings: the Asana PAT stays in the keychain (`ticket-pilot-asana`) or `ASANA_PAT`.
- **Checks**: a check can be switched off with a reason (`"off": true, "reason": "..."`) so it is kept but not run.
  "base 브랜치에서 시험 실행" on the page runs the saved checks on the repo's base branch in the background
  (`tp check --repo R --ref origin/<base>`) and shows the result; turn a check on only after it passes there.

```
bin/tp config get [PATH]                  # everything, or e.g. global.max_active_runs, repo.01_Web.checks
bin/tp config set PATH JSON [--dry-run]   # e.g. set repo.01_Web.deploy.staging.branch '"staging"'; null removes
bin/tp config export [FILE]               # {"settings": ..., "project_map": ...} (the old two file shapes)
bin/tp config import FILE                 # an export or a data/backups/config-*.json; also a bare settings.json / project_map.json
bin/tp config history [SCOPE]             # SCOPE = global, a repo name, or project:<id>
```

Paths: `global.<key>` (settings top level, `global.deploy.deploy_timeout_min`, `global.hidden_projects`),
`repo.<name>.<path|github|description|setup|worktree_base|checks|ci|review|deploy|links>`, `project.<id>.<name|source|asana_gid|repos|confidence|note>` (TP-91: a project has its own id, 1..N repos, Asana optional).

## CLI

```
bin/tp sync | scan | dispatch [--dry-run] [--only KEY]
bin/tp list [--stage queued needs_repo ...] [--json]
bin/tp show KEY
bin/tp assign KEY REPO | skip KEY | unskip KEY
bin/tp adopt KEY <worktree name|path>      # link a worktree you created by hand
bin/tp review-queue --json
bin/tp check [--pr URL [--sha SHA]] [--repo NAME --ref REF]   # tp's own repo checks (see "tp check" below)
bin/tp config get|set|export|import|history ...              # settings (see "Settings" above)
bin/tp review-save --pr URL --sha SHA --verdict approve|changes|blocked --summary "..." --file review.md [--shot a.png ...] [--no-post]
bin/tp screenshot --url URL --out shot.png [--repo WORKTREE] [--device "iPhone 12"] [--js EXPR] [--wait-ms N]   # UI review capture (repo's Playwright, else Orca if installed)
bin/tp tick [--only dispatch review diff cleanup] [--force]   # what launchd runs every minute
bin/tp ws-set --status in-review|todo --comment "..."         # agent completion signal (from its worktree)
bin/tp attach KEY                                             # how to resume a ticket's agent session by hand
bin/tp ticket create --project ID --title TITLE [--body BODY] [--parent KEY | --milestone KEY]
bin/tp milestone create --project ID --title TITLE [--subtask TITLE]...
bin/tp asana comment|create|update|tag ...                    # Asana writes for chat / ticket agents (TP-97; global.agent_asana_write)
bin/tp update [--to TAG|SHA] [--build]                        # fast-forward to the latest release, swap/rebuild UI, restart
bin/tp serve [--port N]
```

## Stages

`open` (not tagged) → `queued` / `needs_repo` / `unmapped` / `waiting_dep` / `skipped` → `dispatched` → `in_progress`
→ `blocked` | `agent_done` → `pr_open` → `reviewed` → (`approving` → `staged`) / `deploying` / `merged` → `done` (verified in production; Asana completed) / `closed`.
Milestones: subtasks `reviewed` → `approving` → `milestone` → `done` with the milestone; milestone tickets start in `collecting`.

AI review verdict `changes` → `rework` (수정 대기, shown in the 대기 column) → the next `tp dispatch` relaunches the
agent in the same worktree/branch with the review as its brief (`prompts/rework_brief.md`) and sets the run
back to in-progress → the agent pushes fixes and marks it in-review → the next review runs on the new head, looking only
at the diff since the last reviewed commit and not re-reporting old or pre-existing findings. After
`review.max_rework_rounds` fix rounds (default 3, 0 = no cap; 2026-10-27, replacing the no-cap rule of 2026-10-19)
a further `changes` verdict goes to the owner as `blocked` instead (event `rework_cap`); their reply relaunches the agent and
restarts the count. Rework takes priority over new tickets for the `max_active_runs` slots.

PR conflicts: any open PR that GitHub reports `CONFLICTING` with its base (any repo, any base branch) is moved to
`conflict` (충돌 대기, shown first in the 대기 column) mechanically by the stage computation on every scan, no LLM gate.
The next `tp dispatch` relaunches the agent in the same worktree with `prompts/conflict_brief.md` (merge `origin/<base>` in,
keep both sides, typecheck/lint, push, mark in-review), ahead of rework and new tickets but within `max_active_runs`
→ the new head gets a normal AI re-review → approve again. Once per head commit (`prs.conflict_sha`); not while the agent
is already working on the branch or the workspace says `BLOCKED` (then the card shows 막힘). A pending dashboard approval
on that ticket is cleared, since the conflicting head can no longer be merged.

## tp check: toothpaste's own PR checks (`ticket_pilot/checks.py`, TP-47)

Evidence order: tp's own check results > the AI review > the agent's report in the PR body. Commands come from
settings, never from a model: `repo.<name>.checks = [{"name", "cmd", "dir"?}]` (`dir` = package
subdirectory, e.g. `app`), or `"ci": true` to read the PR's GitHub checks (`gh pr checks`). A repo with
neither shows "검사 없음" and keeps the old flow.

- The review precheck runs `tp check` before `review-queue`: for every open PR whose agent marked it in-review and whose
  head has no results yet, it starts a detached `tp check --pr` (at most `settings.check.max_parallel` heads at once).
  That run unpacks `git archive <head>` into a temp dir (the worktree and branch are never touched), links the worktree's
  or host checkout's `node_modules` when the lockfile matches (else installs from the lockfile in the temp dir), runs
  each command with `settings.check.timeout_min` (default 10), reruns a failure once (pass on rerun = `flaky`) and
  stores rows in `checks` plus logs in `data/checks/<repo>-<n>-<sha7>-<name>.log`. One run per head (unique key).
- The review queue waits until the head's checks finished and passed (`error` = tp could not run them, e.g. install
  failed: reviewed, but the approval stays gated). Queue items carry `checks` so the reviewer does not rerun them.
- A failed head (`fail`/`timeout`) is stored as a `changes` review with `reviews.source='check'` (event `check_fail`) and
  goes back to the agent through the normal rework flow with the failing command and the last 80 log lines. It counts
  toward `review.max_rework_rounds`. If the agent marks in-review again without a new commit, the owner gets a BLOCKED question.
- Dashboard: check badges on the card (click = log, `GET /api/check-log?id=N`). `release.approval_target` refuses a head
  whose checks are not all pass/flaky; the owner can still approve with a written reason (`tickets.check_override`, that
  head only, event `check_override`, shown on the card). `tp approve KEY --override "reason"` does the same.
- Adding or changing a repo's commands: first `bin/tp check --repo <name> --ref origin/<base>` (no DB writes) and keep only
  the commands that pass on the base branch.

## Sidebar usage bars (`ticket_pilot/usage.py`, `ticket_pilot/codex_usage.py`, TP-32/TP-56)

The sidebar footer always draws four bars — Claude 5시간/7일 and Codex's two limit windows — from the last stored
values. There is no loading, refreshing or "run Claude Code" state: a failed check only updates `error`/`checked_at`
(the card then shows the last check time, reason in the tooltip) and a value is only missing (empty bar, `-`) before
the first successful read.

- Claude: `GET https://api.anthropic.com/api/oauth/usage` with Claude Code's OAuth token, read from the keychain
  read-only and never refreshed (a refresh rotates the refresh token and can log Claude Code out, TP-32). Several
  accounts can share one service name, so the current user's entry (`-a $USER`) is read alongside the plain one and
  the token that expires last wins (TP-56).
- Codex: `codex app-server` over stdio, receiving only `initialize` / `initialized` / `account/rateLimits/read`
  (10s deadline, 1 MiB bound, process-group termination). Ported from 330_ticket_pilot.
- Both are stored in `meta.claude_usage` / `meta.codex_usage` and refreshed at most every `usage.TTL` (300s) by a
  background thread, so `/api/state` never waits on a collection. Tokens, account ids and raw responses are never
  stored, logged or sent to the browser.

## Data

`data/tp.db` (SQLite: tickets, repos, project_map, runs, prs, reviews, events, meta, releases, staging_diffs, cleanups, checks), `data/briefs/`, `data/reviews/`, `data/checks/`.
Gitignored. Asana writes: tasks completed once live in production, and agents' `tp asana` writes (`asana_write` events).

## License

MIT, see [LICENSE](LICENSE).

Before publishing, `scripts/oss-check.sh` fails on personal or company names, home paths and token-shaped strings
(exceptions are listed in the script).
