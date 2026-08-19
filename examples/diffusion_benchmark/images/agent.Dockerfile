FROM node:22-bookworm-slim AS node-runtime

FROM python:3.13-slim-bookworm

COPY --from=node-runtime /usr/local /usr/local

ARG CODEX_VERSION
ARG CLAUDE_CODE_VERSION
RUN test -n "${CODEX_VERSION}" \
    && test -n "${CLAUDE_CODE_VERSION}" \
    && npm install --global \
        "@openai/codex@${CODEX_VERSION}" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

COPY pyproject.toml README.md /opt/brunner-source/
COPY src /opt/brunner-source/src
RUN python -m pip install --no-cache-dir /opt/brunner-source \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin brunner

USER 1000:1000
WORKDIR /tmp
