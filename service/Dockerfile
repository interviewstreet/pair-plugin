# Review service image (PoC2).
#
# The container carries the Claude Code CLI, the pair source, and uv; it does
# not bake any credentials or service-account keys. At runtime the operator
# mounts /data (or sets CLAUDE_CONFIG_DIR elsewhere) with a pre-authenticated
# Claude profile, and injects GOOGLE_APPLICATION_CREDENTIALS or similar.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CLAUDE_CONFIG_DIR=/data/claude

# System deps: git for repo work, curl for the Claude installer, ca-certs for HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# uv for a fast, deterministic install of the project + service extra.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv

# Native Claude Code install; the installer script is the supported path.
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && ln -s /root/.local/bin/claude /usr/local/bin/claude || true

# Non-root runtime user; /data is where the operator mounts writable state.
RUN useradd -m -u 10001 pair \
 && mkdir -p /data/claude \
 && chown -R pair:pair /data

WORKDIR /app
COPY --chown=pair:pair pyproject.toml README.md* ./
COPY --chown=pair:pair src ./src
COPY --chown=pair:pair agents ./agents

USER pair
RUN uv sync --extra service

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "pair.service.app:app", "--host", "0.0.0.0", "--port", "8080"]
