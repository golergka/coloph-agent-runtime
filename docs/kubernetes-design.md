# Kubernetes execution design

Status: accepted design for the 0.2 release series.

## Ownership

The caller creates one worker container and sends one or more agent invocations.
The caller's Python code stays outside the container. Each invocation uses the
existing local ClaudeClient inside the container. The container retains its
workspace and Claude session files between invocations.

The library owns container startup, transport, invocation cancellation, and
container cleanup. Applications own scheduling, retries, provider selection,
budgets, task claims, completion checks, and continuation decisions.

Codex support remains a future feature. This release does not introduce a
shared agent abstraction or implement a scheduler.

## Public interface

```python
backend = KubernetesBackend(namespace="agents", context=None, kubeconfig=None)
with backend.container(template=pod_template, container_name="agent") as container:
    runner = container.claude(executable="claude", auth=auth)
    # runner.run has exactly the local ClaudeClient.run signature.
```

The template is a standard Kubernetes Pod template dictionary or an official
V1PodTemplateSpec. Dockerfiles define images. Image building and publishing
remain outside the library. The selected container starts the bundled worker
service. Standard template fields define resources, Secrets, volumes, service
accounts, image pull credentials, and scheduling constraints.

Explicit lifecycle methods are backend.start, backend.attach, container.status,
container.logs, and container.close. Start accepts an optional stable name.
Starting an existing name fails. Attach reconnects without creating anything.
The context manager starts on entry and closes on exit.

Startup, maximum lifetime, and shutdown timeouts are container lifecycle
parameters. They do not appear in the runner interface. Maximum lifetime is
enforced by the Job deadline. Close waits for Job and Pod deletion. Closing an
already removed container succeeds. A cleanup failure is reported.

## Invocation compatibility

The remote client preserves every local run parameter, its default, and the
ClaudeResult and RunOptions types. Event callbacks execute in the calling
thread. Cancellation uses the caller's threading.Event. Callback failures
cancel the remote invocation before the original callback exception propagates.

Paths resolve inside the container. A missing base_env inherits the worker
environment. An explicit base_env replaces that environment. Authentication
uses the existing Auth types; CLIAuth reads the container's saved login.
Credentials and prompts travel over the authenticated Kubernetes exec channel,
never through Job arguments, annotations, or logs. Kubernetes API TLS checks
remain enabled. Access to pods/exec grants access to the worker's authority.

One container accepts one invocation at a time. Overlapping calls fail without
waiting or starting a second agent. Agent errors retain their known public
exception types and useful fields. Infrastructure and protocol errors use
separate exceptions. Unknown remote errors are reported explicitly.

## Worker and transport

The image runs python -m coloph_agent_runtime.worker. A local Unix socket
accepts versioned JSON messages. Kubernetes exec runs a small bridge in the
selected container. The bridge forwards messages to that socket and streams
JSON event and terminal envelopes back to the caller. No public service or
Pod network endpoint is required.

The worker checks protocol compatibility before execution. The invocation's
duplex connection carries cancellation and heartbeat messages. A lost bridge
connection cancels that invocation. The worker imposes the invocation deadline
independently of the caller. There is no replay or automatic retry.

The Job uses one completion, one parallel worker, backoffLimit=0, and
restartPolicy=Never. Conflicting template settings fail explicitly. Worker
identity is tied to the Job and Pod UIDs, not only their reusable names.
Kubernetes does not guarantee exactly-once business side effects. Application
claims and idempotency remain necessary for durable work.

## Failure and storage

Cancellation stops the current invocation, while leaving a healthy worker
available. Container close terminates the worker and active subprocesses.
Transport loss has an uncertain outcome and must never be presented as a
successful result. Attach supports future calls to a surviving worker; it does
not recover a disconnected invocation's event stream or result.

Ephemeral files disappear with the Pod. Persistent volume contents remain.
Resuming in another container requires the same session files and workspace
paths. An ID alone cannot restore a session.

## Adoption and verification

An application can move its existing invocation loop to the caller and retain
one worker container for that loop. This is a placement change, not a new retry
policy. Adoption must preserve task claims, completion checks, accounting,
session rotation, provider failures, and worker crash handling.

Tests compare local and remote method signatures and execute the same fake
Claude CLI through the worker protocol. Tests cover continuation, events,
credentials, exceptions, timeouts, cancellation, contention, disconnects, and
cleanup. Kubernetes adapter tests check standard templates and lifecycle
operations. An isolated cluster smoke checks real exec transport and Job
cleanup without model calls or application infrastructure.
