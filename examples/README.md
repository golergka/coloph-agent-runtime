# Examples

Run `uv run python examples/application.py` for an offline integration example.
The example uses a fake CLI and explicit application context. It does not require credentials.
Replace the fake executable with `claude` to use an installed CLI and its saved login.

The application owns audit storage, identity, and accounting. The library owns process execution.
