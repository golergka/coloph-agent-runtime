import json
import subprocess
from unittest.mock import MagicMock

import pytest

from coloph_agent_runtime._remote_protocol import RemoteTransportError
from coloph_agent_runtime.docker import DockerBackend, DockerContainer, DockerStatus


def test_limits_and_cleanup_on_body_failure(monkeypatch):
    backend = DockerBackend(context="test")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "immutable-id\n", ""))
    monkeypatch.setattr(backend, "_run", run)
    monkeypatch.setattr(DockerContainer, "status", lambda self: DockerStatus(self.name, self.resource_id, "running", 0))
    monkeypatch.setattr("coloph_agent_runtime.docker.hello", lambda connect: None)
    with pytest.raises(RuntimeError, match="caller"):
        with backend.container(image="worker", name="test"):
            raise RuntimeError("caller")
    command = run.call_args_list[0].args
    assert command[command.index("--cpus") + 1] == "1"
    assert command[command.index("--memory") + 1] == "1g"
    assert command[command.index("--memory-swap") + 1] == "1g"
    assert command[command.index("--pids-limit") + 1] == "256"
    assert run.call_args.args == ("rm", "-f", "immutable-id")


def test_failed_start_cleans_only_created_container(monkeypatch):
    backend = DockerBackend()
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "created-id\n", ""))
    monkeypatch.setattr(backend, "_run", run)
    monkeypatch.setattr(DockerContainer, "status", lambda self: DockerStatus(self.name, self.resource_id, "exited", 1))
    with pytest.raises(RemoteTransportError):
        backend.start(image="worker")
    assert run.call_args.args == ("rm", "-f", "created-id")
    run.reset_mock()
    run.side_effect = subprocess.CalledProcessError(125, ["docker"], stderr="name already in use")
    with pytest.raises(subprocess.CalledProcessError):
        backend.start(image="worker", name="existing")
    assert run.call_count == 1


def test_attach_requires_managed_label_and_pins_identity(monkeypatch):
    backend = DockerBackend()
    record = {"Id": "original", "Name": "/name", "Config": {"Labels": {}}}
    monkeypatch.setattr(backend, "_run", lambda *args: subprocess.CompletedProcess([], 0, json.dumps([record]), ""))
    with pytest.raises(ValueError, match="not managed"):
        backend.attach(name="name")
    record["Config"]["Labels"]["coloph-agent-runtime.managed"] = "true"
    monkeypatch.setattr("coloph_agent_runtime.docker.hello", lambda connect: None)
    handle = backend.attach(name="name")
    record["Id"] = "replacement"
    run = MagicMock()
    monkeypatch.setattr(backend, "_run", run)
    handle.close()
    assert run.call_args.args == ("rm", "-f", "original")


@pytest.mark.parametrize(
    "kwargs", [{"cpus": 0}, {"cpus": float("nan")}, {"memory": ""}, {"pids_limit": 0}, {"startup_timeout_seconds": -1}]
)
def test_limits_rejected_before_launch(kwargs, monkeypatch):
    backend = DockerBackend()
    run = MagicMock()
    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(ValueError):
        backend.start(image="worker", **kwargs)
    run.assert_not_called()


def test_cleanup_does_not_hide_daemon_failure(monkeypatch):
    backend = DockerBackend()
    run = MagicMock(side_effect=subprocess.CalledProcessError(1, ["docker"], stderr="daemon unavailable"))
    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(subprocess.CalledProcessError):
        DockerContainer(backend, "name", "id").close()
