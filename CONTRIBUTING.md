# Contributing

Read [AGENTS.md](AGENTS.md) first: it holds the rules every change follows (Python standard library only, web UI
components and tokens, what never gets committed, Asana and deploy limits). [README.md](README.md) explains the flow.

## Development setup

- Python 3.11+, `git`, `gh`, Node 24 (for `web/`). No pip packages: the backend uses the Python standard library only.
- Work in a branch or a git worktree, not in the checkout that runs your dashboard.
- Offline testing without Asana: see "Offline testing" in `AGENTS.md` (`TP_DB=/tmp/x.db`, `TP_ASANA_FIXTURE`).

## Checks

Run these before opening a PR (CI runs them on every PR):

```bash
python -m unittest discover tests          # backend
cd web && npm ci && npm run build && npm run check   # web UI (typecheck + build + layout checks)
scripts/oss-check.sh                       # no personal or company names, home paths or token-shaped strings
```

## Commits and PRs

- Commit messages: `<type>: <description>` (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`), or
  `[<ticket key>] <description>` for ticket work.
- A PR that changes the user flow (tags, stages, approvals, deploys, screens) also updates `docs/guide.md`.
- Keep PRs scoped to one change and say how you tested it.
