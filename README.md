# coloph-agent-runtime

*Part of Coloph Works: agent-native building blocks for better software.*

Run Claude Code from Python with explicit sessions, streamed events, typed results, and process cleanup.
The core uses only the Python standard library. The optional gateway supports OpenAI-compatible endpoints through LiteLLM.
The optional [Kubernetes backend](docs/kubernetes.md) runs the same client interface in reusable worker containers.

## Future agent support

This release supports Claude Code only. Support for Codex and other agents is future work.
A shared abstraction can be added later. The current API remains Claude-specific, including `ClaudeClient`, authentication, and result types.

## Install

Python 3.11 or later and Claude Code 2.1.215 or later are required.
Local execution supports Linux and macOS. Windows process ownership is not supported.
GitHub Releases is the initial distribution channel.

```sh
uv add "coloph-agent-runtime @ https://github.com/golergka/coloph-agent-runtime/releases/download/v0.2.0/coloph_agent_runtime-0.2.0-py3-none-any.whl"
```

The package includes a skill for coding agents. To install it into a consuming project:

```sh
uv add --dev coloph-install-skills
uv run coloph-install-skills
```

## Run and resume

```python
from pathlib import Path
from coloph_agent_runtime import ClaudeClient, RunOptions

client = ClaudeClient()  # Uses the CLI's saved login.
options = RunOptions(
    allowed_tools=("Read", "Grep", "Glob"),
    model="sonnet",
    max_turns=10,
    timeout_seconds=300,
)
result = client.run("Explain this project", cwd=Path.cwd(), options=options)
print(result.response_text)

followup = client.run(
    "Explain its entry point",
    cwd=Path.cwd(),
    options=options,
    session_id=result.session_id,
    resume=True,
)
```

One call runs one process attempt. The library does not retry or select another provider.
Claude Code owns the session transcript on disk. Keep the working directory and CLI configuration consistent when resuming.

| Session arguments | Behavior |
| --- | --- |
| No `session_id` | Generate a UUID and create a session. `resume` is ignored. |
| ID with `resume=False` | Create a session with the supplied ID. |
| ID with `resume=True` | Continue the existing session. |

`allowed_tools`, `disallowed_tools`, and `permission_mode` map to CLI flags.
They are not an application authorization system or an operating-system sandbox.
An empty allowlist retains the extracted wrapper's explicit `Agent` deny rule. It does not claim to disable every CLI tool.

## Credentials and application context

```python
from coloph_agent_runtime import ClaudeClient, OAuthAuth

client = ClaudeClient(auth=OAuthAuth(token=token))
result = client.run(
    prompt,
    cwd=repo,
    options=options,
    env_overrides={"APP_PRINCIPAL": "worker", "APP_INVOCATION_ID": invocation_id},
    on_event=record_audit_event,
)
```

Applications own principals, tool authorization, provider selection, budget policy, retries, and audit persistence.
`env_overrides` carries application context to the child without interpretation.
The library copies the parent environment, removes provider credential variables, then applies explicit authentication.
Use `base_env` to supply the entire base environment instead. Neither mapping is mutated.
Credential variables in `env_overrides` are rejected. Pass credentials through `auth`.

Authentication types:

- `CLIAuth()` uses the saved CLI login and removes inherited API credentials.
- `OAuthAuth(token=...)` uses an explicit OAuth token.
- `AnthropicAuth(base_url=..., auth_token=...)` uses an Anthropic-compatible endpoint. `api_key` is an alternative to `auth_token`.
- `OpenAIAuth(base_url=..., model=..., auth_token=...)` starts a local gateway. Install the `gateway` extra first.

`max_budget_usd` maps to the CLI's per-run budget flag. Application-wide spending limits remain the caller's responsibility.
No dotenv files are loaded. No credential store or database is required by the library.

## Events and logging

`on_event` receives decoded event dictionaries on the calling thread, in stream order.
Callbacks must return promptly. Their exceptions propagate after process cleanup.
A process deadline cannot interrupt Python code blocked inside a callback.
`capture_events=False` avoids retaining the event history in the result.

```python
from coloph_agent_runtime import JsonlRecorder

with open("events.jsonl", "w", encoding="utf-8") as stream:
    result = client.run(prompt, cwd=repo, options=options, on_event=JsonlRecorder(stream))
```

The caller opens and closes the stream. The library does not print model output or create logs by default.
Internal diagnostics use the `coloph_agent_runtime` Python logger.
Events, stderr, transcripts, and gateway diagnostics can contain prompt or provider data. Applications own their storage and access controls.

## Results and failures

`ClaudeResult` contains the session ID, response text, structured output, events, stderr, usage, cost, timing, and terminal event.
Usage and cost fields remain `None` when the CLI does not report them.
`json_schema` maps to `--json-schema`. The library returns `structured_output` without application-specific schema validation.

A successful return requires a terminal `result` event with `subtype="success"`, a session ID, and no `is_error` flag.
Missing or duplicate terminal events raise `ClaudeProtocolError` with captured events and stderr.
An unsuccessful terminal result raises `ClaudeResultError`, even when the process exits zero.
Successful execution does not establish that the agent completed the business task correctly.

Nonzero process exits raise `ClaudeInvocationError` or its authentication, subscription, usage-limit, and provider-configuration subclasses.
These exceptions retain the subprocess command and diagnostics. Usage-limit exceptions can include `rate_limit_reset_at`.
Timeouts raise `subprocess.TimeoutExpired`. Cancellation raises `ClaudeCancelledError`.
Missing executables and filesystem failures raise standard OS exceptions. Unsupported CLI versions raise `ClaudeCodeVersionError`.

## Process ownership

The timeout starts before CLI version preflight and gateway startup. Cleanup can add bounded time after that deadline.
The client reads stdout, stderr, and stdin concurrently to avoid pipe deadlocks.
Each invocation starts a new POSIX process group. Cleanup kills that group and reaps the direct child.
Tools that deliberately detach from the group are outside this ownership boundary.
The library does not replace the application's signal handlers.

Pass a `threading.Event` as `cancel`. When set, it requests interruption, followed by forced termination after at most one second.
The API is synchronous. Cancelling an `asyncio.to_thread()` await does not cancel the subprocess.
Async hosts must set the cancellation event and wait for cleanup before abandoning the worker thread.

Transcript repair is disabled by default. `RunOptions(repair_transcript=True, ...)` repairs standalone empty-thinking records before resume.
Mixed-content corrupt records fail without rewriting the file. The repair helper targets the default `~/.claude/projects` layout.

## Optional gateway

```sh
uv add "coloph-agent-runtime[gateway] @ https://github.com/golergka/coloph-agent-runtime/releases/download/v0.2.0/coloph_agent_runtime-0.2.0-py3-none-any.whl"
```

The gateway owns temporary configuration, LiteLLM, and a local response-validation proxy for one call.
It converts JSON messages to Anthropic SSE and rejects malformed or empty responses.
Both processes stop when the call ends. Endpoint compatibility still depends on the model and provider.
The release tests exercise protocol handling without paid provider calls.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and releases, and [examples](examples/README.md) for standalone integration code.
MIT licensed. Release versions remain on `0.2.*`.
