"""Real Job/exec smoke in a disposable kind cluster. No model or application services."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from kubernetes import client

from coloph_agent_runtime import CodexRunOptions, RunOptions
from coloph_agent_runtime.kubernetes import KubernetesBackend
from coloph_agent_runtime.runner import ClaudeCancelledError

ROOT = Path(__file__).resolve().parents[1]

FAKE = """#!/usr/local/bin/python
import json, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("2.1.215")
    raise SystemExit(0)
prompt = sys.argv[sys.argv.index("-p") + 1]
flag = "--resume" if "--resume" in sys.argv else "--session-id"
sid = sys.argv[sys.argv.index(flag) + 1]
if flag == "--resume":
    assert Path(sid).read_text() == "session"
Path(sid).write_text("session")
print(json.dumps({"type":"assistant", "message":{"content":[]}}), flush=True)
if prompt == "sleep":
    time.sleep(120)
if prompt == "pipes":
    assert len(sys.stdin.read()) == 200000
    sys.stderr.write("e" * 200000)
print(json.dumps({"type":"result","subtype":"success","session_id":sid,"result":"done",
                  "usage":{"input_tokens":1,"output_tokens":1}}), flush=True)
"""


def command(*args: str, timeout: int = 300) -> None:
    subprocess.run(args, check=True, timeout=timeout, cwd=ROOT)


def main() -> None:
    name = "agent-smoke-" + uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="agent-k8s-") as temporary:
        directory = Path(temporary)
        kubeconfig = directory / "kubeconfig"
        fake = directory / "claude"
        fake.write_text(FAKE)
        dockerfile = directory / "Dockerfile"
        dockerfile.write_text(
            "FROM python:3.12-slim\nWORKDIR /package\n"
            "COPY pyproject.toml README.md LICENSE ./\nCOPY src ./src\n"
            "RUN pip install --no-cache-dir . && useradd --create-home agent\n"
            "USER agent\nWORKDIR /home/agent\n"
            'ENTRYPOINT ["python", "-m", "coloph_agent_runtime.worker"]\n'
        )
        image = name + ":test"
        print("Building worker image", flush=True)
        command("docker", "build", "--progress=plain", "-f", str(dockerfile), "-t", image, ".")
        try:
            print("Creating isolated kind cluster", flush=True)
            command("kind", "create", "cluster", "--name", name, "--kubeconfig", str(kubeconfig), "--wait", "120s")
            command("kind", "load", "docker-image", image, "--name", name)
            backend = KubernetesBackend(namespace="default", kubeconfig=str(kubeconfig))
            with backend._api() as api:
                client.CoreV1Api(api).create_namespaced_config_map(
                    "default",
                    {
                        "metadata": {"name": "fake-cli"},
                        "data": {"claude": FAKE, "codex": (ROOT / "tests/fixtures/fake_codex.py").read_text()},
                    },
                )
            template = {
                "spec": {
                    "containers": [
                        {
                            "name": "agent",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "volumeMounts": [{"name": "cli", "mountPath": "/cli"}],
                        }
                    ],
                    "volumes": [{"name": "cli", "configMap": {"name": "fake-cli", "defaultMode": 365}}],
                }
            }
            print("Checking real exec, continuation, cancellation, attach and cleanup", flush=True)
            with backend.container(template=template, lifetime_seconds=180) as container:
                runner = container.claude(executable="/cli/claude")
                options = RunOptions(allowed_tools=(), timeout_seconds=30)
                events = []
                result = runner.run("ok", cwd=Path("/home/agent"), options=options, on_event=events.append)
                assert result.response_text == "done" and len(events) == 2
                attached = backend.attach(name=container.name)
                resumed = attached.claude(executable="/cli/claude").run(
                    "ok", cwd=Path("/home/agent"), options=options, session_id=result.session_id, resume=True
                )
                assert resumed.session_id == result.session_id
                cancel = threading.Event()
                try:
                    runner.run(
                        "sleep",
                        cwd=Path("/home/agent"),
                        options=options,
                        cancel=cancel,
                        on_event=lambda event: cancel.set(),
                    )
                    raise AssertionError("Cancellation did not raise")
                except ClaudeCancelledError:
                    pass
                assert runner.run("ok", cwd=Path("/home/agent"), options=options).response_text == "done"
                pipes = runner.run("pipes", cwd=Path("/home/agent"), options=options, stdin_text="a" * 200000)
                assert len(pipes.stderr) == 200000
                codex = container.codex(executable="/cli/codex")
                codex_options = CodexRunOptions(timeout_seconds=30)
                codex_result = codex.run("ok", cwd=Path("/home/agent"), options=codex_options)
                assert codex_result.response_text == "done"
                assert (
                    codex.run(
                        "ok", cwd=Path("/home/agent"), options=codex_options, session_id=codex_result.session_id
                    ).session_id
                    == codex_result.session_id
                )
            assert container.status().phase == "removed"
            container.close()
            print("Kubernetes smoke passed", flush=True)
        finally:
            command("kind", "delete", "cluster", "--name", name, timeout=120)


if __name__ == "__main__":
    main()
