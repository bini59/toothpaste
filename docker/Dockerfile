# ticket-pilot in Docker (TP-164), optional: the default is still the host install (launchd / systemd).
# Build from the repository root: cd docker && docker compose up -d --build (also loads compose.override.yaml)
# No secrets in the image: GitHub / Asana tokens go to the data volume from the dashboard, agent logins to the home volume.

# web/dist
FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM node:24-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates curl gnupg openssh-client procps \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable \
 && npm install -g --no-audit --no-fund @anthropic-ai/claude-code @openai/codex \
 && npm cache clean --force

# the image's non-root user is node (uid 1000); the volumes take these folders' owner when they are first created.
# tp mode docker (TP-165, docker/compose.override.yaml) builds with the host user's uid/gid and home path, so the
# host folders it mounts at the same paths (repos, worktrees, ~/.claude, ~/.codex) are the container user's own.
ARG TP_UID=1000
ARG TP_GID=1000
ARG TP_HOME=/home/node
ENV HOME=/home/node
RUN groupmod -o -g "$TP_GID" node && usermod -o -u "$TP_UID" -g "$TP_GID" node \
 && mkdir -p /opt/tp/data /home/node/tp-work/worktrees /home/node/tp-work/repos "$TP_HOME" \
 && chown -R node:node /opt/tp /home/node "$TP_HOME"
COPY --chown=node:node . /opt/tp
COPY --from=web --chown=node:node /web/dist /opt/tp/web/dist

ARG TP_RELEASE_VERSION
USER node
WORKDIR /opt/tp
ENV TP_RELEASE_VERSION=$TP_RELEASE_VERSION \
    TP_MODE=docker \
    TP_HOST=0.0.0.0 \
    TP_PORT=3032 \
    TP_LOG_DIR=/opt/tp/data/logs \
    TP_SECRETS_FILE=/opt/tp/data/secrets/secrets.json \
    PYTHONPATH=/opt/tp \
    PATH=/opt/tp/bin:/usr/local/bin:/usr/bin:/bin
EXPOSE 3032
CMD ["python3", "-P", "-m", "ticket_pilot.cli", "run"]
