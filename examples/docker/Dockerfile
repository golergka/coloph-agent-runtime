# Build from the repository root. No host credentials enter this build context.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
ARG CODEX_VERSION=0.144.1
ARG CLAUDE_CODE_VERSION=2.1.215
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install -g \
    @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
WORKDIR /package
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home agent && mkdir /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "coloph_agent_runtime.worker"]
