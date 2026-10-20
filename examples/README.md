# Examples

Run `uv run python examples/application.py` for an offline integration example.
The example uses fake Claude and Codex CLIs and explicit application context. It does not require credentials.
Replace a fake executable with `claude` or `codex` to use the installed CLI and its saved login.

For real container execution, use [the Dockerfile](docker/Dockerfile) and the
[Codex and Docker guide](../docs/codex.md). The image supports both agents.

The application owns audit storage, identity, and accounting. The library owns process execution.
