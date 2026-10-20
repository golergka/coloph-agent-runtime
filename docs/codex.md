# Codex execution

CodexClient uses `codex exec --json`. The tested CLI version is 0.144.1.
The run method has the same arguments as ClaudeClient.run, with agent-specific
CodexRunOptions and CodexResult types. Claude APIs remain unchanged.

```python
from pathlib import Path
from coloph_agent_runtime import CodexClient, CodexRunOptions

runner = CodexClient()  # Uses your saved Codex login, including ChatGPT plans.
options = CodexRunOptions(timeout_seconds=300, sandbox="read-only")
result = runner.run("Explain this project", cwd=Path.cwd(), options=options)
followup = runner.run("Explain its tests", cwd=Path.cwd(), options=options, session_id=result.session_id, resume=True)
```

Codex assigns new session IDs. Supplying a session_id with resume=False fails;
the CLI cannot create a thread with a caller-assigned ID. Ephemeral calls cannot
resume sessions. Callbacks receive native Codex event dictionaries on the calling
thread. Results expose final text, thread ID, token usage, stderr and events.
No dollar-cost estimate is invented from subscription usage.

Options include model, effort, sandbox, timeout_seconds, json_schema,
capture_events, skip_git_repo_check, ignore_user_config, and ephemeral.
CLI-specific Claude options such as allowed_tools and max_budget_usd are not
silently translated to Codex. Codex's approval policy is non-interactive.
The default sandbox is read-only; choose workspace-write for edits.

Missing/duplicate completion raises CodexProtocolError. Failed turns or nonzero
exit raise CodexInvocationError. Cancellation raises CodexCancelledError.
Timeout raises subprocess.TimeoutExpired. Each call owns and cleans up its
process group. Tools that detach from that group are outside this boundary.

## Local Docker

Build the supplied image from the repository root:

```sh
docker build -f examples/docker/Dockerfile -t my-agent-worker:0.3.0 .
```

This image includes both CLIs and the worker service. DockerBackend uses the
installed Docker CLI; no Python Docker SDK is required.
Use only a trusted Docker daemon or cluster: its administrators can read the
credentials supplied to a worker.

```python
from pathlib import Path
from coloph_agent_runtime import CodexRunOptions, CodexSubscriptionAuth
from coloph_agent_runtime.docker import DockerBackend

auth = CodexSubscriptionAuth.from_local()
with DockerBackend().container(image="my-agent-worker:0.3.0") as container:
    runner = container.codex(auth=auth)
    result = runner.run(
        "Say hello",
        cwd=Path("/workspace"),
        options=CodexRunOptions(skip_git_repo_check=True, ignore_user_config=True),
    )
```

The caller runs outside the container. Sequential calls reuse its workspace and
sessions. container.claude() remains available in the same container. One
invocation runs at a time across both agents.

Docker workers default to one CPU, 1 GiB memory with no extra swap allowance,
and 256 PIDs. Override cpus, memory, and pids_limit explicitly. Native Docker
mount specifications are accepted through mounts, for example
`("type=bind,src=/absolute/project,dst=/workspace",)`. Mount only the needed
project directories. environment contains non-secret container configuration.
Use the auth objects for credentials, not environment or build arguments.

The backend also exposes start and attach(name=...). Container handles provide
status, logs and close. Handles use immutable Docker IDs, so a reused name does
not redirect cleanup. The context manager removes its worker on exit.
There is no automatic retry, restart or image build. If the caller is killed,
the invocation heartbeat expires, but the idle Docker container needs explicit
cleanup. Docker containers have no Kubernetes Job lifetime deadline.

Docker's default Linux security configuration can prevent Codex from creating
an additional nested sandbox for shell tools. For trusted tasks inside a
container, explicitly select sandbox="danger-full-access" when relying on the
container boundary. This does not give the container access to unmounted host
files. Never mount the Docker socket into an agent worker.

## Subscription credentials

CodexCLIAuth uses the execution environment's saved login. An optional
codex_home selects another home; credential_store selects file, keyring or auto.
API credential environment variables are removed to prevent accidental API
billing when saved-login authentication is selected.

CodexSubscriptionAuth.from_local() reads only auth.json from CODEX_HOME or
~/.codex. It sends that cache through the private worker connection, without
mounting your home, configuration, history or other credentials. It requires a
file-based ChatGPT login. Keyring-only users can create a separate file-backed
login with the official CLI; the library does not scrape OS credential stores.

The worker keeps Codex session state in an isolated home for that account.
Refreshed credentials return in a private protocol frame, never as agent events.
The helper updates the original auth file atomically with mode 0600 when the
source still matches the starting snapshot. A changed source fails explicitly
instead of overwriting another login. The auth object retains the refreshed
cache for recovery with auth.save(path).

For a separate credential file, use CodexSubscriptionAuth.from_file(path).
That method does not write back automatically; persist updates with save(path).
Do not use the same copied refresh credentials concurrently in independent
clients. The library cannot coordinate refreshes by the desktop app or other
CLIs. For unattended long-running work, prefer a separate login cache created
with the official device-code flow and persist it between runs.

Transport loss can prevent a refreshed cache from reaching the caller. Treat
the outcome as uncertain; reconcile or sign in again instead of blindly
retrying. Never put auth.json in source control, image layers, tickets or logs.

OpenAI documents saved CLI login and copying the cache for headless use:
[authentication](https://learn.chatgpt.com/docs/auth) and
[non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode).

## Kubernetes

The existing Kubernetes container exposes the same codex() method. Standard Pod
templates still configure images, volumes and resources. Use the same
CodexSubscriptionAuth helper or a persistent, pre-authenticated Codex home.
See [Kubernetes execution](kubernetes.md). Version 0.3 uses worker protocol 2;
update client and worker images together.
