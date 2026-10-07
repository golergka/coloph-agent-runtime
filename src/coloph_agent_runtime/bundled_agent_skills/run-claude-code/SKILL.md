---
name: run-claude-code
description: Integrate headless Claude Code execution into Python applications with coloph-agent-runtime, including sessions, event storage, credentials, and process cancellation.
---

# Run Claude Code from Python

Use `ClaudeClient.run(prompt, cwd=..., options=RunOptions(allowed_tools=(...)))` for one synchronous attempt.
Keep provider selection, authorization, accounting, audit storage, and retries in the application.
Pass application context through `env_overrides`. Pass provider credentials through `auth`, never environment overrides.
Choose `CLIAuth`, `OAuthAuth`, `AnthropicAuth`, or `OpenAIAuth`. The last requires the `gateway` extra.

Keep `session_id` and `resume` separate. An absent ID creates a session. A supplied ID resumes unless `resume=False`.
Keep the same working directory and CLI configuration when resuming.
Use event callbacks for persistence. `JsonlRecorder` takes a caller-owned text stream and does not close it.
Callbacks run synchronously. Keep them bounded. Callback errors propagate after child cleanup.

Set a finite timeout. For cancellation, pass a `threading.Event` and set it when the application must stop the run.
Cancelling a thread await alone does not stop the subprocess. Wait for the synchronous call to finish cleanup.
Do not add automatic retries around agent tool side effects without an application-specific replay policy.

Use `json_schema` to request structured output. Validate the returned value against the application's schema before use.
A successful terminal result proves execution completion, not business-task correctness.
Transcript repair is opt-in and supports only the default Claude projects directory.
Keep credentials and private data out of logs and public fixtures.

For Kubernetes execution, install the `kubernetes` extra on the caller.
Use KubernetesBackend.container(template=...) with a native Pod template.
The image must run `python -m coloph_agent_runtime.worker` and contain Claude Code.
Use container.claude() for the same run interface. Paths and inherited environment belong to the container.
Reuse the container for continuation calls. The caller owns retries and completion checks.
Keep Kubernetes exec access restricted. Close the container when its caller-owned loop ends.
Treat RemoteTransportError as an uncertain outcome. Do not automatically replay agent side effects.
