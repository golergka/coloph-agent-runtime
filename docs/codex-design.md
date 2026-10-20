# Codex and Docker execution for 0.3.0

CodexClient runs one `codex exec --json` invocation. Its run arguments match the
Claude client, with CodexRunOptions and CodexResult as agent-specific types.
The container exposes both claude() and codex(). Neither client retries.

Codex creates its own thread ID. An existing session_id can be resumed; assigning
an ID to a new thread is unsupported and must fail explicitly. Session files
remain in CODEX_HOME. Results preserve native events, final text and token usage.
Missing or failed terminal events must not return success.

CodexCLIAuth uses the execution environment's saved login. An explicit
CodexSubscriptionAuth.from_local() reads only the local auth.json file and sends
it through the existing private worker transport. It must not mount the entire
host Codex directory or put tokens in image layers, arguments or logs. Refreshed
credentials must survive sequential invocations and be exportable for subsequent
containers. File-based login is the documented headless path; keyring-only login
requires a separate file-backed login, not undocumented keychain scraping.

DockerBackend owns reusable local worker containers with the same lifecycle as
the Kubernetes backend. Docker exec carries the worker protocol. Defaults limit
each worker to one CPU, 1 GiB RAM and 256 PIDs. Tests run one container at a time.
No local Kubernetes cluster is needed for the requested Docker validation.

Verification must include deterministic local and Docker tests for both agents,
session reuse, events, failures, deadlines, cancellation, large I/O, and cleanup.
A small live Codex subscription smoke must prove actual authentication and a
successful invocation inside Docker. The release requires existing Claude tests,
artifact installation checks, public CI and downloaded release verification.

References: https://learn.chatgpt.com/docs/non-interactive-mode and
https://learn.chatgpt.com/docs/auth .
