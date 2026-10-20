"""Bounded local Docker containers using the same worker protocol as Kubernetes."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import uuid4

from ._remote_client import RemoteClaudeClient, hello
from ._remote_codex import RemoteCodexClient
from ._remote_protocol import RemoteTransportError
from .auth import Auth
from .codex import CodexCLIAuth
from .codex_auth import CodexSubscriptionAuth


@dataclass(frozen=True)
class DockerStatus:
    name: str
    resource_id: str
    phase: str
    exit_code: int | None


class DockerBackend:
    def __init__(self, *, context: str | None = None, executable: str = "docker") -> None:
        self.command = [executable] + (["--context", context] if context else [])

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*self.command, *args], capture_output=True, text=True, check=True, timeout=30)

    @contextmanager
    def container(
        self,
        *,
        image: str,
        name: str | None = None,
        cpus: float = 1,
        memory: str = "1g",
        pids_limit: int = 256,
        mounts: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        startup_timeout_seconds: float = 30,
    ) -> Iterator[DockerContainer]:
        container = self.start(
            image=image,
            name=name,
            cpus=cpus,
            memory=memory,
            pids_limit=pids_limit,
            mounts=mounts,
            environment=environment,
            startup_timeout_seconds=startup_timeout_seconds,
        )
        try:
            yield container
        finally:
            container.close()

    def start(
        self,
        *,
        image: str,
        name: str | None = None,
        cpus: float = 1,
        memory: str = "1g",
        pids_limit: int = 256,
        mounts: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        startup_timeout_seconds: float = 30,
    ) -> DockerContainer:
        import math

        if not math.isfinite(cpus) or cpus <= 0 or pids_limit < 1 or not memory:
            raise ValueError("CPU, memory and PID limits are required")
        if not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be finite and positive")
        name = name or "agent-" + uuid4().hex
        command = [
            "run",
            "-d",
            "--init",
            "--name",
            name,
            "--label",
            "coloph-agent-runtime.managed=true",
            "--cpus",
            str(cpus),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--pids-limit",
            str(pids_limit),
        ]
        for mount in mounts:
            command.extend(["--mount", mount])
        for key, value in (environment or {}).items():
            command.extend(["--env", f"{key}={value}"])
        command.append(image)
        resource_id = self._run(*command).stdout.strip()
        result = DockerContainer(self, name, resource_id)
        try:
            deadline = time.monotonic() + startup_timeout_seconds
            while time.monotonic() < deadline:
                if result.status().phase != "running":
                    raise RemoteTransportError("Docker worker exited before startup completed")
                try:
                    hello(result._connect)
                    return result
                except RemoteTransportError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            raise RemoteTransportError("Docker worker startup timed out")
        except BaseException:
            result.close()
            raise

    def attach(self, *, name: str) -> DockerContainer:
        record = json.loads(self._run("inspect", name).stdout)[0]
        if record["Config"].get("Labels", {}).get("coloph-agent-runtime.managed") != "true":
            raise ValueError("Container is not managed by this library")
        result = DockerContainer(self, record["Name"].lstrip("/"), record["Id"])
        hello(result._connect)
        return result


class DockerContainer:
    def __init__(self, backend: DockerBackend, name: str, resource_id: str) -> None:
        self.backend, self.name, self.resource_id = backend, name, resource_id

    def _connect(self) -> _PipeChannel:
        return _PipeChannel(
            [
                *self.backend.command,
                "exec",
                "-i",
                self.resource_id,
                "python",
                "-m",
                "coloph_agent_runtime.worker",
                "bridge",
            ]
        )

    def claude(self, *, executable: str = "claude", auth: Auth | None = None) -> RemoteClaudeClient:
        return RemoteClaudeClient(self._connect, executable=executable, auth=auth)

    def codex(
        self, *, executable: str = "codex", auth: CodexCLIAuth | CodexSubscriptionAuth | None = None
    ) -> RemoteCodexClient:
        return RemoteCodexClient(self._connect, executable=executable, auth=auth)

    def status(self) -> DockerStatus:
        try:
            record = json.loads(self.backend._run("inspect", self.resource_id).stdout)[0]
        except subprocess.CalledProcessError as exc:
            if "No such" in exc.stderr:
                return DockerStatus(self.name, self.resource_id, "removed", None)
            raise
        return DockerStatus(self.name, self.resource_id, record["State"]["Status"], record["State"]["ExitCode"])

    def logs(self, *, tail_lines: int = 200) -> str:
        if tail_lines < 1:
            raise ValueError("tail_lines must be positive")
        result = self.backend._run("logs", "--tail", str(tail_lines), self.resource_id)
        return result.stdout + result.stderr

    def close(self) -> None:
        try:
            self.backend._run("rm", "-f", self.resource_id)
        except subprocess.CalledProcessError as exc:
            if "No such" not in exc.stderr:
                raise


class _PipeChannel:
    def __init__(self, command: list[str]) -> None:
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.pending = b""
        self._readers: list[Any] = [self.proc.stdout, self.proc.stderr]

    def write_stdin(self, data: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data.encode())
        self.proc.stdin.flush()

    def update(self, timeout: float) -> None:
        if not self._readers:
            return
        readable, _, _ = select.select(self._readers, [], [], timeout)
        for reader in readable:
            data = os.read(reader.fileno(), 65536)
            if not data:
                self._readers.remove(reader)
            if reader is self.proc.stdout:
                self.pending += data

    def read_stdout(self) -> str:
        # Protocol JSON escapes non-ASCII characters, so chunks are independently decodable.
        data, self.pending = self.pending.decode(), b""
        return data

    def is_open(self) -> bool:
        return bool(self._readers)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                pipe.close()
