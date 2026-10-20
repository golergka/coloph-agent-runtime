# Build from the repository root after `uv build`.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
ARG CLAUDE_CODE_VERSION=2.1.215
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
COPY dist/coloph_agent_runtime-0.3.0-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir /tmp/coloph_agent_runtime-0.3.0-py3-none-any.whl && \
    useradd --create-home agent && mkdir /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "coloph_agent_runtime.worker"]
