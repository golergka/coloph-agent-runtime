# Run agents in Kubernetes

Install the client with the `kubernetes` extra. Use the wheel URL from the
matching GitHub release; the package is not published to PyPI.

```sh
uv add "coloph-agent-runtime[kubernetes] @ https://github.com/golergka/coloph-agent-runtime/releases/download/v0.3.0/coloph_agent_runtime-0.3.0-py3-none-any.whl"
```

## Prepare an image

Build your image with a normal Dockerfile. Install this package and Claude Code
2.1.215 or later. The image must provide `python` on PATH. Run as a non-root
user with a writable home directory and workspace. Use this entrypoint:

```dockerfile
ENTRYPOINT ["python", "-m", "coloph_agent_runtime.worker"]
```

The worker uses Python's standard library. It does not need the Kubernetes
extra. For OpenAIAuth, install the `gateway` extra in the worker image.
The caller and image must support the same worker protocol version.

The library does not build or publish images. For local containers and a
Dockerfile with both CLIs, see the [Codex and Docker guide](codex.md).

The repository includes an [example Dockerfile](../examples/kubernetes/Dockerfile).
After `uv build`, build it from the repository root:

```sh
docker build -f examples/kubernetes/Dockerfile -t example/review-worker:0.3.0 .
```

## Start a container

```python
from pathlib import Path
import yaml
from coloph_agent_runtime import OAuthAuth, RunOptions
from coloph_agent_runtime.kubernetes import KubernetesBackend

backend = KubernetesBackend(namespace="agents", context="my-cluster")
template = yaml.safe_load(Path("agent-pod.yaml").read_text())

with backend.container(template=template, container_name="agent") as container:
    runner = container.claude(auth=OAuthAuth(token=token))
    result = runner.run(
        "Inspect the workspace",
        cwd=Path("/workspace"),
        options=RunOptions(allowed_tools=("Read", "Grep", "Glob")),
    )
    followup = runner.run(
        "Explain the entry point",
        cwd=Path("/workspace"),
        options=RunOptions(allowed_tools=("Read", "Grep", "Glob")),
        session_id=result.session_id,
        resume=True,
    )
```

All Python code in this example executes in the caller. The Claude processes
execute inside the container. The two calls share workspace and session files.
No automatic retry or continuation occurs.

`agent-pod.yaml` is a native Pod template, not a full Pod or Job manifest:

```yaml
metadata:
  labels:
    app: review-worker
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: agent
      image: example/review-worker:0.3.0
      resources:
        requests: {cpu: 250m, memory: 512Mi}
        limits: {cpu: "2", memory: 4Gi}
      volumeMounts:
        - name: workspace
          mountPath: /workspace
  volumes:
    - name: workspace
      persistentVolumeClaim:
        claimName: review-workspace
```

An official `kubernetes.client.V1PodTemplateSpec` is also accepted. The library
copies the template without changing the caller's object. This release accepts
one worker container and rejects init containers and sidecars. Other native
fields, including volumes, Secrets, security contexts and scheduling rules,
pass through to Kubernetes. Conflicting lifecycle settings fail before creation.

## Connection and permissions

`KubernetesBackend(namespace=..., context=None, kubeconfig=None)` uses
in-cluster credentials when available, otherwise the default kubeconfig.
An explicit context or kubeconfig selects kubeconfig authentication.
TLS certificate verification is required.

The caller needs these namespaced permissions:

```yaml
rules:
  - apiGroups: [batch]
    resources: [jobs]
    verbs: [create, get, delete]
  - apiGroups: [""]
    resources: [pods]
    verbs: [list]
  - apiGroups: [""]
    resources: [pods/log]
    verbs: [get]
  - apiGroups: [""]
    resources: [pods/exec]
    verbs: [get, create]
```

Agent credentials use the existing auth objects. Saved login files can be
mounted for CLIAuth. Credentials are sent through Kubernetes exec, not stored
in Job arguments or annotations. Anyone with exec access to the worker has
access to that worker's authority. Protect kubeconfig and volume access.

## Lifecycle interface

`backend.start(...)` and `backend.container(...)` accept the same arguments:

| Argument | Default | Meaning |
| --- | --- | --- |
| `template` | required | Native Pod template |
| `container_name` | `"agent"` | Selected worker container |
| `name` | generated | Job name; existing names fail |
| `startup_timeout_seconds` | `120` | Startup wait budget, plus bounded API calls |
| `lifetime_seconds` | `3600` | Job deadline, including idle time |
| `shutdown_timeout_seconds` | `30` | Cleanup wait budget |

```python
container = backend.start(template=template, name="review-worker-123")
container = backend.attach(name="review-worker-123")
status = container.status()
diagnostics = container.logs(tail_lines=200)
container.close()
```

`attach` reconnects to a surviving managed worker. It does not replay or recover
an interrupted invocation. `status` returns a ContainerStatus with name, UID,
phase, Pod name, exit code and reason. `logs` returns worker diagnostics; agent
events use `on_event`. `close` terminates active work and waits for Job and Pod
removal. It does not delete persistent volume claims. Repeated close calls are
safe. Job UID preconditions prevent deletion of a replacement with the same name.

Jobs have no Kubernetes retries. The deadline stops abandoned workers. Completed
Jobs expire after five minutes. The context manager always attempts cleanup,
including when the caller raises an exception. Cleanup failure is reported.

## Runner compatibility and failures

The remote `run` signature and defaults exactly match ClaudeClient.run.
RunOptions, ClaudeResult, callbacks, session arguments, and authentication types
remain unchanged. `cwd` and executable paths resolve inside the container.
With `base_env=None`, the invocation inherits the worker environment.
Callbacks execute in the caller's thread. A callback must return promptly.

Cancellation stops the active invocation and preserves a healthy container for
later calls. Overlapping invocations raise WorkerBusyError. Known agent errors
retain their local exception types. Unexpected worker exceptions become
RemoteExecutionError. Connection or protocol failures raise RemoteTransportError
with an uncertain outcome; the library never retries them.

A heartbeat lease cancels work after ten seconds without client messages.
The invocation deadline is enforced independently inside the worker. Transport
loss can prevent cleanup confirmation, so applications must reconcile their
business state before retrying. Kubernetes does not guarantee exactly-once side
effects, even with Job retries disabled.

Protocol frames have a 32 MiB limit. Large captured histories can exceed it;
use capture_events=False and stream events to caller-owned storage.
Session files survive calls in one container. Resuming in another container
requires persistent session files and the same workspace paths.

Use container.codex() for Codex execution with the same interface as the local
CodexClient. The image must include Codex. See the [Codex guide](codex.md) for
subscription authentication and options. Version 0.3 requires worker protocol
2; rebuild worker images when upgrading the client.
See the [design](kubernetes-design.md) for ownership and application adoption.
