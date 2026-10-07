# Changelog

## 0.2.0

- Add optional Kubernetes worker containers with native Pod templates and user-built images.
- Preserve the local Claude run signature, results, events, authentication and cancellation interface remotely.
- Reuse workspace and session files across sequential invocations in one container.
- Add explicit start, attach, status, logs and close operations with UID-based cleanup protection.
- Add a versioned worker protocol over authenticated Kubernetes exec and disconnect cancellation.
- Keep invocation retries, continuation policy and scheduling in the application.
- Test worker subprocesses, API parity and real Kubernetes Job/exec transport in a disposable kind cluster.
- Codex and a standalone Docker execution backend remain future features.

## 0.1.0

- Add synchronous Claude Code calls with explicit sessions, credentials, tool flags, budgets, and deadlines.
- Add ordered event callbacks, caller-owned JSONL recording, and typed results and failures.
- Add cancellation, concurrent pipe handling, process-group cleanup, and opt-in transcript repair.
- Add an optional LiteLLM gateway for OpenAI-compatible endpoints.
- Include standalone examples, an agent skill, offline subprocess tests, and installed-artifact checks.
