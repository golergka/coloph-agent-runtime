from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from coloph_agent_runtime.kubernetes import Container, ContainerError, KubernetesBackend, _job


def template():
    return {"spec": {"containers": [{"name": "agent", "image": "worker:0.2.0"}]}}


def test_native_template_and_no_mutation():
    original = template()
    original["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "workspace"}}]
    before = deepcopy(original)
    job = _job(original, "agent", "test", 120)
    assert original == before
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["parallelism"] == 1
    assert job["spec"]["activeDeadlineSeconds"] == 120
    assert job["spec"]["template"]["spec"]["volumes"] == original["spec"]["volumes"]
    typed = client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[client.V1Container(name="agent", image="x")]))
    assert _job(typed, "agent", "test", 120)["spec"]["template"]["spec"]["containers"][0]["image"] == "x"


@pytest.mark.parametrize(
    "field,value", [("restartPolicy", "Always"), ("activeDeadlineSeconds", 20), ("initContainers", [{"name": "init"}])]
)
def test_conflicting_template_rejected(field, value):
    value_template = template()
    value_template["spec"][field] = value
    with pytest.raises(ValueError):
        _job(value_template, "agent", "test", 120)


def backend():
    result = object.__new__(KubernetesBackend)
    result.namespace = "test"
    result.configuration = client.Configuration()
    result._api = MagicMock()
    return result


def test_start_failure_cleans_created_job(monkeypatch):
    batch = MagicMock()
    batch.create_namespaced_job.return_value = NS(metadata=NS(uid="uid"))
    monkeypatch.setattr(client, "BatchV1Api", lambda api: batch)
    monkeypatch.setattr(Container, "_ready", MagicMock(side_effect=ContainerError("startup")))
    cleanup = MagicMock()
    monkeypatch.setattr(Container, "close", cleanup)
    with pytest.raises(ContainerError, match="startup"):
        backend().start(template=template())
    cleanup.assert_called_once()


def test_existing_name_is_not_deleted(monkeypatch):
    batch = MagicMock()
    batch.create_namespaced_job.side_effect = ApiException(status=409)
    monkeypatch.setattr(client, "BatchV1Api", lambda api: batch)
    cleanup = MagicMock()
    monkeypatch.setattr(Container, "close", cleanup)
    with pytest.raises(ApiException):
        backend().start(template=template(), name="existing")
    cleanup.assert_not_called()


def test_cleanup_uid_precondition_and_idempotence(monkeypatch):
    batch, core = MagicMock(), MagicMock()
    batch.delete_namespaced_job.side_effect = ApiException(status=404)
    batch.read_namespaced_job.side_effect = ApiException(status=404)
    core.list_namespaced_pod.return_value.items = []
    monkeypatch.setattr(client, "BatchV1Api", lambda api: batch)
    monkeypatch.setattr(client, "CoreV1Api", lambda api: core)
    container = Container(backend(), "old", "old-uid", "agent", 2)
    container.close()
    container.close()
    assert batch.delete_namespaced_job.call_args.kwargs["body"]["preconditions"] == {"uid": "old-uid"}


def test_replaced_job_is_not_used(monkeypatch):
    batch = MagicMock()
    batch.read_namespaced_job.return_value = NS(metadata=NS(uid="replacement"))
    monkeypatch.setattr(client, "BatchV1Api", lambda api: batch)
    container = Container(backend(), "reused-name", "original", "agent", 2)
    with pytest.raises(ContainerError, match="another container"):
        container.status()


def test_replaced_or_duplicate_pod_is_not_used(monkeypatch):
    batch, core = MagicMock(), MagicMock()
    batch.read_namespaced_job.return_value = NS(metadata=NS(uid="job"))
    pod = NS(metadata=NS(uid="replacement", owner_references=[NS(uid="job")]))
    core.list_namespaced_pod.return_value.items = [pod]
    monkeypatch.setattr(client, "BatchV1Api", lambda api: batch)
    monkeypatch.setattr(client, "CoreV1Api", lambda api: core)
    container = Container(backend(), "name", "job", "agent", 2)
    container._pod_uid = "original"
    with pytest.raises(ContainerError, match="Pod was replaced"):
        container.status()
    core.list_namespaced_pod.return_value.items = [pod, pod]
    with pytest.raises(ContainerError, match="multiple Pods"):
        container.status()
