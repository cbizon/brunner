FROM node:22-bookworm-slim AS node-runtime

FROM python:3.13-slim-bookworm

COPY --from=node-runtime /usr/local /usr/local

ARG CODEX_VERSION
ARG KUBECTL_VERSION
ARG TARGETARCH
RUN test -n "${CODEX_VERSION}" \
    && test -n "${KUBECTL_VERSION}" \
    && test -n "${TARGETARCH}" \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && curl --fail --location --silent --show-error \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
        --output /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl \
    && apt-get purge --yes --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /opt/brunner-source/
COPY src /opt/brunner-source/src
RUN python -m pip install --no-cache-dir /opt/brunner-source \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin brunner

COPY examples /opt/brunner/examples
ENV PYTHONPATH=/opt/brunner

USER 1000:1000
WORKDIR /tmp
