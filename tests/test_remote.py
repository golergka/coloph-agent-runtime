from __future__ import annotations

import inspect
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from coloph_agent_runtime import ClaudeClient, OAuthAuth, RunOptions
from coloph_agent_runtime._remote_client import RemoteClaudeClient, hello
from coloph_agent_runtime._remote_protocol import PROTOCOL_VERSION, WorkerBusyError
from coloph_agent_runtime.errors import ClaudeUsageLimitError
from coloph_agent_runtime.runner import ClaudeCancelledError, ClaudeProtocolError, ClaudeResultError


class SocketChannel:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.connect(path)
        self.pending = b""
        self.open = True

    def write_stdin(self, data):
        self.socket.sendall(data.encode())

    def update(self, timeout):
        readable, _, _ = select.select([self.socket], [], [], timeout)
        if readable:
            data = self.socket.recv(65536)
            if not data:
                self.open = False
            self.pending += data

    def read_stdout(self):
        # Test fixture output is ASCII JSON.
        result, self.pending = self.pending.decode(), b""
        return result

    def is_open(self):
        return self.open

    def close(self):
        self.socket.close()


@pytest.fixture
def worker(tmp_path):
    # macOS Unix socket paths have a short maximum length.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="car-") as directory:
        path = str(Path(directory) / "s")
        proc = subprocess.Popen(
            [sys.executable, "-m", "coloph_agent_runtime.worker", "--socket", path],
            env={**os.environ, "HOME": str(tmp_path)},
        )
        try:
            deadline = time.monotonic() + 5
            while not Path(path).exists():
                assert proc.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)

            def connect():
                return SocketChannel(path)

            hello(connect)
            yield connect
        finally:
            proc.terminate()
            proc.wait(timeout=20)


def test_exact_run_signature():
    assert inspect.signature(RemoteClaudeClient.run) == inspect.signature(ClaudeClient.run)


def test_remote_result_sessions_events_auth(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude), auth=OAuthAuth("secret"))
    events = []
    result = client.run("structured", cwd=tmp_path, options=RunOptions(allowed_tools=()), on_event=events.append)
    assert result.structured_output == {"answer": 42}
    assert result.events == events
    assert result.cost_usd == 0.01
    context = json.loads((tmp_path / "context.json").read_text())
    assert context["CLAUDE_CODE_OAUTH_TOKEN"] == "secret"
    resumed = client.run(
        "ok",
        cwd=tmp_path,
        options=RunOptions(allowed_tools=(), capture_events=False),
        session_id=result.session_id,
        resume=True,
    )
    assert resumed.session_id == result.session_id
    assert resumed.events == []
    assert "--resume" in json.loads((tmp_path / "argv.json").read_text())


@pytest.mark.parametrize(
    "prompt,error",
    [("missing", ClaudeProtocolError), ("failure", ClaudeUsageLimitError), ("terminal_error", ClaudeResultError)],
)
def test_remote_error_types(worker, fake_claude, tmp_path, prompt, error):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))
    with pytest.raises(error) as caught:
        client.run(prompt, cwd=tmp_path, options=RunOptions(allowed_tools=()))
    if prompt == "failure":
        assert caught.value.rate_limit_reset_at.timestamp() == 1900000000
        assert "diagnostic" in caught.value.stderr


def test_timeout_and_reuse(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))
    with pytest.raises(subprocess.TimeoutExpired):
        client.run("sleep", cwd=tmp_path, options=RunOptions(allowed_tools=(), timeout_seconds=2))
    assert client.run("ok", cwd=tmp_path, options=RunOptions(allowed_tools=())).response_text == "done"


def test_cancellation_and_busy(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))
    cancel = threading.Event()
    errors = []

    def run():
        try:
            client.run("sleep", cwd=tmp_path, options=RunOptions(allowed_tools=()), cancel=cancel)
        except ClaudeCancelledError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "child.pid").exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(WorkerBusyError):
            client.run("ok", cwd=tmp_path, options=RunOptions(allowed_tools=()))
    finally:
        cancel.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert len(errors) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "child.pid").read_text()), 0)


def test_callback_failure_reaps_agent(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))

    def callback(event):
        raise LookupError("caller failure")

    with pytest.raises(LookupError, match="caller failure"):
        client.run("callback", cwd=tmp_path, options=RunOptions(allowed_tools=()), on_event=callback)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "child.pid").read_text()), 0)


def test_large_stdin_and_stderr(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))
    result = client.run("pipes", cwd=tmp_path, options=RunOptions(allowed_tools=()), stdin_text="a" * 200000)
    assert len(result.stderr) == 200000
    assert (tmp_path / "input_size").read_text() == "200000"


def test_disconnect_reaps_agent(worker, fake_claude, tmp_path):
    channel = worker()
    channel.write_stdin(
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "op": "run",
                "prompt": "sleep",
                "cwd": str(tmp_path),
                "options": asdict(RunOptions(allowed_tools=())),
                "executable": str(fake_claude),
                "auth": {"kind": "CLIAuth", "values": {}},
                "session_id": None,
                "resume": True,
                "env_overrides": None,
                "base_env": None,
                "stdin_text": None,
            }
        )
        + "\n"
    )
    deadline = time.monotonic() + 5
    while not (tmp_path / "child.pid").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    pid = int((tmp_path / "child.pid").read_text())
    channel.close()
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_explicit_environment_and_callback_thread(worker, fake_claude, tmp_path):
    client = RemoteClaudeClient(worker, executable=str(fake_claude))
    caller = threading.get_ident()
    threads = []
    client.run(
        "ok",
        cwd=tmp_path,
        options=RunOptions(allowed_tools=()),
        base_env={"PATH": os.environ["PATH"], "APP_ROLE": "base", "ANTHROPIC_API_KEY": "discard"},
        env_overrides={"APP_ROLE": "override"},
        on_event=lambda event: threads.append(threading.get_ident()),
    )
    assert threads and set(threads) == {caller}
    context = json.loads((tmp_path / "context.json").read_text())
    assert context["APP_ROLE"] == "override"
    assert context["ANTHROPIC_API_KEY"] is None


def test_incompatible_protocol_rejected_before_execution(worker):
    channel = worker()
    try:
        channel.write_stdin('{"op":"run","version":999}\n')
        output = ""
        deadline = time.monotonic() + 5
        while "\n" not in output:
            assert time.monotonic() < deadline
            channel.update(0.1)
            output += channel.read_stdout()
        frame = json.loads(output)
        assert frame["type"] == "error"
        assert "Incompatible" in frame["error"]["message"]
    finally:
        channel.close()
