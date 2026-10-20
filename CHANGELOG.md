# Changelog

## 0.3.0

- Add Codex execution with native streamed events, session resume, structured JSON, token usage, deadlines and cancellation.
- Preserve each agent's local run signature in Docker and Kubernetes workers.
- Add a Docker CLI backend with explicit CPU, memory and PID limits, attach, logs and deterministic cleanup.
- Add portable ChatGPT subscription authentication from a local file login, with private refresh transport and guarded writeback.
- Include a Dockerfile with both CLIs and deterministic container smoke tests. Test real Codex subscription execution and resume locally.
- Upgrade the worker protocol to version 2. Rebuild worker images when upgrading clients.
- Keep existing Claude APIs and caller-owned retry policy unchanged.

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
