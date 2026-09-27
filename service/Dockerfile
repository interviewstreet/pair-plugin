# Review service image (PoC2).
#
# The container carries the Claude Code CLI, the pair source, and uv; it does
# not bake any credentials or service-account keys. At runtime the operator
# mounts /data (or sets CLAUDE_CONFIG_DIR elsewhere) with a pre-authenticated
# Claude profile, and injects GOOGLE_APPLICATION_CREDENTIALS or similar.
#
# Build context is the repository root, not this directory:
#   docker build -f service/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CLAUDE_CONFIG_DIR=/data/claude

# System deps: git for repo work, curl for the Claude installer, ca-certs for HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Everything below runs as `pair`, so uv and the Claude
# CLI install under its own $HOME: installing them as root would leave them in
# /root, which is mode 700 and unreadable to this user.
RUN useradd -m -u 10001 pair \
 && mkdir -p /app /data/claude \
 && chown -R pair:pair /app /data
USER pair
ENV HOME=/home/pair \
    PATH=/home/pair/.local/bin:$PATH

# uv for a fast, deterministic install of the project + service extra.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Native Claude Code install; the installer script is the supported path.
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /app
# uv.lock comes along so `uv sync --locked` resolves to the pinned versions and
# fails the build if pyproject.toml and the lock have drifted.
COPY --chown=pair:pair pyproject.toml uv.lock ./
# `src` is the pair package; `agents` holds the four writer prompts that
# jobs.py reads through load_agent_prompt(). Without agents/ every section
# fails at run time, so it is part of the image, not a mount.
COPY --chown=pair:pair src ./src
COPY --chown=pair:pair agents ./agents

RUN uv sync --locked --no-dev --extra service

# Fail the build rather than the first request if the layout above ever drifts.
RUN .venv/bin/python -c "\
from pair.service.main import resolve_agents_dir;\
import pair.service.app, pair.service.jobs;\
print('agents:', resolve_agents_dir({}))"

EXPOSE 8080
# The venv is invoked directly rather than through `uv run`, which would try to
# re-resolve the project on every container start.
CMD ["/app/.venv/bin/uvicorn", "pair.service.main:app", "--host", "0.0.0.0", "--port", "8080"]
