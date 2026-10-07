# Contributing

## Checks

```sh
uv sync --locked
uv run pytest
uv run mypy
uv run python -m ruff check .
uv run python -m ruff format --check .
uv build
uv run python scripts/smoke_artifacts.py
```

Tests run real local subprocesses with a fake CLI. No account, database, server, or paid model call is required.
Gateway contract tests use an in-process HTTP client. Optional gateway startup tests require the `gateway` extra.
Artifact smoke installs each distribution in a clean environment outside the checkout and runs a standalone example.

```sh
uv sync --locked --extra gateway
uv run --no-sync python scripts/smoke_gateway.py
```

The gateway smoke starts both gateway processes against a local Responses API stub, then checks their cleanup.

## Release

1. Keep the version on `0.2.*` and update CHANGELOG.md.
2. Run the checks and review the changes.
3. Commit and push the tested source to the public main branch.
4. Wait for CI on that commit.
5. Create the matching immutable `v0.2.*` tag.
6. Publish the wheel and source distribution in a GitHub release.
7. Run the artifact smoke against the downloaded public artifacts.

GitHub Releases is the distribution channel. PyPI publication requires separate publisher configuration.
Never move published tags or replace released files. Never publish credentials or private repository references.

## Kubernetes checks

Unit tests use real local worker processes and a fake Claude CLI. Signature
parity is checked against the local client. For real Job and exec transport,
install Docker, kind and kubectl, then run:

```sh
uv run python scripts/smoke_kubernetes.py
```

The smoke creates a uniquely named local cluster with its own kubeconfig.
It builds a worker image, checks invocations and cleanup, then deletes the
cluster. It uses a fake agent and makes no model calls.
