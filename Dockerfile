# One image for the control plane (fledge serve) and every step container
# (fledge step-exec). Teams extend it with FROM to add their own package and
# the toolchains their repositories need.
FROM python:3.13-slim

# Pinned because the harnesses depend on these CLIs' flags and output formats.
ARG CLAUDE_CODE_VERSION=2.1.331
ARG CODEX_VERSION=0.172.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gh git nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:${PATH}"

# Claude Code refuses to skip permission prompts as root, so steps run as an
# ordinary user. A new workspace volume takes its ownership from this directory.
RUN useradd --create-home --uid 1000 fledge \
    && mkdir /workspace \
    && chown fledge:fledge /workspace
USER fledge
WORKDIR /workspace

CMD ["fledge", "serve"]
