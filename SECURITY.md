# Security

## Threat model

toothpaste runs coding agents on your machine with your credentials. Treat access to its dashboard like shell access.

- **Agents are not sandboxed.** Ticket, review and chat agents (`claude` / `codex`) run as your user, with your `gh`
  login and tp's GitHub token, and can run any command your user can. Ticket text, PR comments and repository content
  reach them as data; a malicious ticket can still try to steer an agent.
- **Dashboard access = command execution.** Anyone who can use the dashboard can start a chat agent that runs commands on
  this machine, approve merges and start production deploys.
- **Who can reach the dashboard**
  - Default (host install): it listens on `127.0.0.1` only; requests from this machine are trusted without a login.
  - 로컬망 공유 (LAN sharing): binds to `0.0.0.0`; every LAN request needs a GitHub login (Device Flow) from an active
    member of the configured org. Only `127.0.0.1` can change that setting.
  - Docker mode (`docker/`): every request, `127.0.0.1` included, needs a GitHub login from `TP_ADMINS` or `TP_ORG`.
  - Public exposure: use Docker mode with the Cloudflare Tunnel profile **and** a Cloudflare Access application in front
    of it (see README, "외부 공개"). Never expose a host install directly.
- **Secrets** never go into the settings DB or logs:
  - macOS: the login keychain (`ticket-pilot-asana` for the Asana PAT, `ticket-pilot-github` for tp's GitHub token); `ASANA_PAT`
    in the environment wins.
  - Linux and Docker: `~/.config/ticket-pilot/secrets.json` (mode 600 in a 700 folder; tp refuses to read it when the
    mode is wider). Docker keeps it in the `tp-data` volume.
  - A custom GitHub OAuth client id (`data/github-oauth.json` or keychain `ticket-pilot-github-oauth`) is not a secret.
- **Asana** is read-only except for the writes listed in `AGENTS.md` (completion after a verified production deploy, and
  agent writes through `tp asana`, which `global.agent_asana_write` turns off).

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository (private vulnerability
reporting / GitHub Security Advisories). Do not open a public issue. Include the version (dashboard sidebar, or
`git describe --tags` in the checkout), how toothpaste runs (macOS, Linux, Docker) and steps to reproduce.
