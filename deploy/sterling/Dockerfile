# The RoboRobo service: `program serve` with git, ssh, rsync, uv and Claude
# Code. RoboRobo itself is not baked in: the entrypoint clones it onto the
# volume, laid out like the scientist's Mac, so paths rendered into program
# files (the brief, the Claude Code settings) are the same here.
FROM python:3.12-slim-bookworm

# 2.1.280 or newer is needed for Opus 5.5.
ARG CLAUDE_CODE_VERSION=2.1.326
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client rsync ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

RUN groupadd -g 7474 roborobo && useradd -u 7474 -g 7474 -d /Users/bizon -M -s /bin/sh roborobo
COPY entrypoint.sh /usr/local/bin/roborobo-service

ENV HOME=/Users/bizon \
    ROBOROBO_HOST=sterling \
    TMPDIR=/Users/bizon/.tmp \
    UV_CACHE_DIR=/Users/bizon/.cache/uv
USER 7474
ENTRYPOINT ["/usr/local/bin/roborobo-service"]
