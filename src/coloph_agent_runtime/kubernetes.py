"""Optional Kubernetes container lifecycle; templates use the native Pod schema."""

from __future__ import annotations

import copy
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import uuid4

from kubernetes import client, config  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]
from kubernetes.stream import stream  # type: ignore[import-untyped]

from ._remote_client import Channel, RemoteClaudeClient, hello
from ._remote_codex import RemoteCodexClient
from ._remote_protocol import RemoteExecutionError, RemoteTransportError, WorkerBusyError
from .auth import Auth
from .codex import CodexCLIAuth
from .codex_auth import CodexSubscriptionAuth

__all__ = [
    "KubernetesBackend",
    "Container",
    "ContainerStatus",
    "RemoteExecutionError",
    "RemoteTransportError",
    "WorkerBusyError",
    "ContainerError",
]

OWNER_LABEL = "coloph-agent-runtime/managed"
CONTAINER_ANNOTATION = "coloph-agent-runtime/container"


class ContainerError(RemoteExecutionError):
    """A container could not start, be identified, or be removed."""


@dataclass(frozen=True)
class ContainerStatus:
    name: str
    uid: str
    phase: str
    pod_name: str | None
    exit_code: int | None = None
    reason: str | None = None


def _positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _job(template: Any, container_name: str, name: str, lifetime_seconds: int) -> dict[str, Any]:
    if not isinstance(template, dict):
        template = client.ApiClient().sanitize_for_serialization(template)
    template = copy.deepcopy(template)
    if not isinstance(template, dict) or "spec" not in template:
        raise ValueError("Supply a Kubernetes Pod template with a spec")
    spec = template["spec"]
    if spec.get("restartPolicy", "Never") != "Never":
        raise ValueError("The worker requires restartPolicy: Never")
    containers = spec.get("containers", [])
    if len(containers) != 1 or containers[0].get("name") != container_name:
        raise ValueError("The template must contain exactly one selected worker container")
    if spec.get("initContainers") or spec.get("ephemeralContainers"):
        raise ValueError("Init containers and sidecars are not supported in 0.2")
    if not containers[0].get("image"):
        raise ValueError("The worker container requires an image")
    if containers[0].get("stdin") or containers[0].get("tty"):
        raise ValueError("The worker entrypoint must not require stdin or a TTY")
    if "activeDeadlineSeconds" in spec:
        raise ValueError("Use lifetime_seconds instead of a Pod deadline")
    spec["restartPolicy"] = "Never"
    metadata = template.setdefault("metadata", {})
    if metadata.get("name") or metadata.get("generateName") or metadata.get("ownerReferences"):
        raise ValueError("The library owns Pod names and owner references")
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "labels": {OWNER_LABEL: "true"},
            "annotations": {CONTAINER_ANNOTATION: container_name},
        },
        "spec": {
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "activeDeadlineSeconds": lifetime_seconds,
            "ttlSecondsAfterFinished": 300,
            "template": template,
        },
    }


class KubernetesBackend:
    def __init__(self, *, namespace: str, context: str | None = None, kubeconfig: str | None = None) -> None:
        if not namespace:
            raise ValueError("namespace must not be empty")
        configuration = client.Configuration()
        if context is not None or kubeconfig is not None:
            config.load_kube_config(config_file=kubeconfig, context=context, client_configuration=configuration)
        else:
            try:
                config.load_incluster_config(client_configuration=configuration)
            except config.ConfigException:
                config.load_kube_config(client_configuration=configuration)
        if not configuration.verify_ssl:
            raise ValueError("Kubernetes API TLS verification must be enabled")
        self.namespace = namespace
        self.configuration = configuration

    def _api(self) -> Any:
        return client.ApiClient(self.configuration)

    @contextmanager
    def container(
        self,
        *,
        template: Any,
        container_name: str = "agent",
        name: str | None = None,
        startup_timeout_seconds: float = 120,
        lifetime_seconds: int = 3600,
        shutdown_timeout_seconds: float = 30,
    ) -> Iterator[Container]:
        container = self.start(
            template=template,
            container_name=container_name,
            name=name,
            startup_timeout_seconds=startup_timeout_seconds,
            lifetime_seconds=lifetime_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
        try:
            yield container
        finally:
            container.close()

    def start(
        self,
        *,
        template: Any,
        container_name: str = "agent",
        name: str | None = None,
        startup_timeout_seconds: float = 120,
        lifetime_seconds: int = 3600,
        shutdown_timeout_seconds: float = 30,
    ) -> Container:
        for key, value in (
            ("startup_timeout_seconds", startup_timeout_seconds),
            ("lifetime_seconds", lifetime_seconds),
            ("shutdown_timeout_seconds", shutdown_timeout_seconds),
        ):
            _positive(value, key)
        if not isinstance(lifetime_seconds, int):
            raise ValueError("lifetime_seconds must be an integer")
        name = name or "agent-" + uuid4().hex
        body = _job(template, container_name, name, lifetime_seconds)
        with self._api() as api:
            job = client.BatchV1Api(api).create_namespaced_job(self.namespace, body, _request_timeout=10)
        container = Container(self, name, job.metadata.uid, container_name, shutdown_timeout_seconds)
        try:
            container._ready(startup_timeout_seconds)
        except BaseException:
            # Startup owns the newly created Job, including interrupted starts.
            container.close()
            raise
        return container

    def attach(self, *, name: str, shutdown_timeout_seconds: float = 30) -> Container:
        _positive(shutdown_timeout_seconds, "shutdown_timeout_seconds")
        with self._api() as api:
            job = client.BatchV1Api(api).read_namespaced_job(name, self.namespace, _request_timeout=10)
        if (job.metadata.labels or {}).get(OWNER_LABEL) != "true":
            raise ContainerError("Job is not managed by coloph-agent-runtime")
        selected = (job.metadata.annotations or {}).get(CONTAINER_ANNOTATION)
        if not selected:
            raise ContainerError("Job does not identify its worker container")
        container = Container(self, name, job.metadata.uid, selected, shutdown_timeout_seconds)
        container._ready(10)
        return container


class Container:
    def __init__(self, backend: KubernetesBackend, name: str, uid: str, container_name: str, shutdown: float) -> None:
        self.backend, self.name, self.uid = backend, name, uid
        self.container_name = container_name
        self._shutdown = shutdown
        self._pod_uid: str | None = None

    def _observe(self) -> tuple[Any, Any]:
        with self.backend._api() as api:
            try:
                job = client.BatchV1Api(api).read_namespaced_job(self.name, self.backend.namespace, _request_timeout=10)
            except ApiException as exc:
                if exc.status == 404:
                    return None, None
                raise
            if job.metadata.uid != self.uid:
                raise ContainerError("Job name now refers to another container")
            pods = (
                client.CoreV1Api(api)
                .list_namespaced_pod(
                    self.backend.namespace, label_selector=f"job-name={self.name}", _request_timeout=10
                )
                .items
            )
        owned = [p for p in pods if any(r.uid == self.uid for r in (p.metadata.owner_references or []))]
        if len(owned) > 1:
            raise ContainerError("Job has multiple Pods; refusing ambiguous execution")
        pod = owned[0] if owned else None
        if pod is not None and self._pod_uid is not None and pod.metadata.uid != self._pod_uid:
            raise ContainerError("Worker Pod was replaced; automatic recovery is disabled")
        return job, pod

    def status(self) -> ContainerStatus:
        job, pod = self._observe()
        if job is None:
            return ContainerStatus(self.name, self.uid, "removed", None)
        if pod is None:
            return ContainerStatus(self.name, self.uid, "pending", None)
        statuses = pod.status.container_statuses or []
        selected = next((s for s in statuses if s.name == self.container_name), None)
        terminated = selected.state.terminated if selected is not None else None
        return ContainerStatus(
            self.name,
            self.uid,
            pod.status.phase.lower(),
            pod.metadata.name,
            terminated.exit_code if terminated else None,
            terminated.reason if terminated else pod.status.reason,
        )

    def _ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job, pod = self._observe()
            if job is None:
                raise ContainerError("Worker Job was removed during startup")
            if pod is not None:
                if pod.status.phase in {"Failed", "Succeeded"}:
                    raise ContainerError("Worker exited before startup completed")
                if pod.status.phase == "Running":
                    self._pod_uid = pod.metadata.uid
                    try:
                        hello(self._connect)
                        return
                    except RemoteTransportError:
                        if time.monotonic() >= deadline:
                            raise
            time.sleep(0.2)
        raise ContainerError("Worker startup timed out")

    def _connect(self) -> Channel:
        _, pod = self._observe()
        if pod is None or pod.status.phase != "Running":
            raise ContainerError("Worker container is not running")
        # stream mutates ApiClient.request; each connection gets its own client.
        api = self.backend._api()
        try:
            channel = stream(
                client.CoreV1Api(api).connect_get_namespaced_pod_exec,
                pod.metadata.name,
                self.backend.namespace,
                container=self.container_name,
                command=["python", "-m", "coloph_agent_runtime.worker", "bridge"],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
                _request_timeout=10,
            )
            return _ExecChannel(channel, api)
        except BaseException:
            api.close()
            raise

    def claude(self, *, executable: str = "claude", auth: Auth | None = None) -> RemoteClaudeClient:
        return RemoteClaudeClient(self._connect, executable=executable, auth=auth)

    def codex(
        self, *, executable: str = "codex", auth: CodexCLIAuth | CodexSubscriptionAuth | None = None
    ) -> RemoteCodexClient:
        return RemoteCodexClient(self._connect, executable=executable, auth=auth)

    def logs(self, *, tail_lines: int = 200) -> str:
        if tail_lines < 1:
            raise ValueError("tail_lines must be positive")
        _, pod = self._observe()
        if pod is None:
            return ""
        with self.backend._api() as api:
            return str(
                client.CoreV1Api(api).read_namespaced_pod_log(
                    pod.metadata.name,
                    self.backend.namespace,
                    container=self.container_name,
                    tail_lines=tail_lines,
                    _request_timeout=10,
                )
            )

    def close(self) -> None:
        deadline = time.monotonic() + self._shutdown
        with self.backend._api() as api:
            batch = client.BatchV1Api(api)
            try:
                batch.delete_namespaced_job(
                    self.name,
                    self.backend.namespace,
                    body={
                        "propagationPolicy": "Foreground",
                        "gracePeriodSeconds": 5,
                        "preconditions": {"uid": self.uid},
                    },
                    _request_timeout=min(10, self._shutdown),
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
            while time.monotonic() < deadline:
                pods = (
                    client.CoreV1Api(api)
                    .list_namespaced_pod(
                        self.backend.namespace,
                        label_selector=f"job-name={self.name}",
                        _request_timeout=max(0.1, min(5, deadline - time.monotonic())),
                    )
                    .items
                )
                owned = [p for p in pods if any(r.uid == self.uid for r in (p.metadata.owner_references or []))]
                try:
                    job = batch.read_namespaced_job(
                        self.name,
                        self.backend.namespace,
                        _request_timeout=max(0.1, min(5, deadline - time.monotonic())),
                    )
                    gone = job.metadata.uid != self.uid
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                    gone = True
                if gone and not owned:
                    return
                time.sleep(0.2)
        raise ContainerError(f"Timed out waiting for container {self.name} cleanup")


class _ExecChannel:
    def __init__(self, channel: Any, api: Any) -> None:
        self.channel, self.api = channel, api

    def write_stdin(self, data: str) -> None:
        self.channel.write_stdin(data)

    def update(self, timeout: float) -> None:
        self.channel.update(timeout=timeout)
        # Diagnostic stderr is deliberately not mixed into agent event frames.
        self.channel.read_stderr(timeout=0)

    def read_stdout(self) -> str:
        return str(self.channel.read_stdout(timeout=0))

    def is_open(self) -> bool:
        return bool(self.channel.is_open())

    def close(self) -> None:
        try:
            self.channel.close()
        finally:
            self.api.close()
