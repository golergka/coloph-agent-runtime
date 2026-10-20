import inspect
import json
import os
import subprocess
import threading

import pytest
from test_remote import worker as worker

from coloph_agent_runtime import (
    CodexCancelledError,
    CodexClient,
    CodexInvocationError,
    CodexProtocolError,
    CodexRunOptions,
    CodexSubscriptionAuth,
)
from coloph_agent_runtime._remote_codex import RemoteCodexClient


def test_run_signature_parity():
    assert inspect.signature(CodexClient.run) == inspect.signature(RemoteCodexClient.run)


@pytest.fixture(params=["local", "remote"])
def codex(request, worker, fake_codex):
    if request.param == "local":
        return CodexClient(executable=str(fake_codex))
    return RemoteCodexClient(worker, executable=str(fake_codex))


def test_result_and_resume(codex, tmp_path):
    events = []
    result = codex.run("ok", cwd=tmp_path, options=CodexRunOptions(), on_event=events.append)
    assert result.response_text == "done"
    assert result.input_tokens == 10 and result.cache_read_tokens == 4
    assert result.events == events
    assert result.terminal_event["type"] == "turn.completed"
    resumed = codex.run("ok", cwd=tmp_path, options=CodexRunOptions(capture_events=False), session_id=result.session_id)
    assert resumed.session_id == result.session_id and resumed.events == []


@pytest.mark.parametrize(
    "prompt,error",
    [("missing", CodexProtocolError), ("duplicate", CodexProtocolError), ("failure", CodexInvocationError)],
)
def test_failures(codex, tmp_path, prompt, error):
    with pytest.raises(error):
        codex.run(prompt, cwd=tmp_path, options=CodexRunOptions())


def test_deadline(codex, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        codex.run("sleep", cwd=tmp_path, options=CodexRunOptions(timeout_seconds=0.5))
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "codex.pid").read_text()), 0)


def test_cancellation(codex, tmp_path):
    cancel = threading.Event()
    with pytest.raises(CodexCancelledError):
        codex.run("sleep", cwd=tmp_path, options=CodexRunOptions(), cancel=cancel, on_event=lambda event: cancel.set())


def test_schema_and_stdin(codex, tmp_path):
    result = codex.run("structured", cwd=tmp_path, options=CodexRunOptions(json_schema={"type": "object"}))
    assert result.structured_output == {"answer": 42}
    result = codex.run("pipes", cwd=tmp_path, options=CodexRunOptions(), stdin_text="x" * 200000)
    assert len(result.stderr) == 200000


def test_callback_failure_cleans_process_and_allows_next_call(codex, tmp_path):
    def fail(event):
        if event["type"] == "item.completed":
            raise LookupError("callback failed")

    with pytest.raises(LookupError, match="callback failed"):
        codex.run("callback", cwd=tmp_path, options=CodexRunOptions(timeout_seconds=10), on_event=fail)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "codex.pid").read_text()), 0)
    assert codex.run("ok", cwd=tmp_path, options=CodexRunOptions()).response_text == "done"


def test_reject_unsupported_session_arguments(codex, tmp_path):
    with pytest.raises(ValueError):
        codex.run("ok", cwd=tmp_path, options=CodexRunOptions(), session_id="chosen", resume=False)
    with pytest.raises(ValueError):
        codex.run("ok", cwd=tmp_path, options=CodexRunOptions(ephemeral=True), session_id="chosen")


def test_remote_subscription_refresh_survives_callback_failure(worker, fake_codex, tmp_path):
    auth = CodexSubscriptionAuth(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "secret", "account_id": "remote-test"}})
    )

    def fail(event):
        if event["type"] == "item.completed":
            raise LookupError("callback failed")

    client = RemoteCodexClient(worker, executable=str(fake_codex), auth=auth)
    with pytest.raises(LookupError, match="callback failed"):
        client.run("refresh", cwd=tmp_path, options=CodexRunOptions(), on_event=fail)
    assert "refreshed-test-token" in auth._auth_json


def test_subscription_refresh_and_secrecy(tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "login"
    home.mkdir()
    source = home / "auth.json"
    source.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "secret-test-token", "account_id": "test"}})
    )
    auth = CodexSubscriptionAuth.from_local(codex_home=home)
    assert "secret-test-token" not in repr(auth)
    result = CodexClient(executable=str(fake_codex), auth=auth).run("refresh", cwd=tmp_path, options=CodexRunOptions())
    assert result.response_text == "done"
    assert json.loads(source.read_text())["tokens"]["access_token"] == "refreshed-test-token"
    assert source.stat().st_mode & 0o777 == 0o600
    assert "refreshed-test-token" not in json.dumps(result.events)


def test_subscription_changed_source_not_overwritten(tmp_path):
    data = {"auth_mode": "chatgpt", "tokens": {"access_token": "old", "account_id": "test"}}
    source = tmp_path / "auth.json"
    source.write_text(json.dumps(data))
    auth = CodexSubscriptionAuth.from_local(codex_home=tmp_path)
    source.write_text("new user login")
    data["tokens"]["access_token"] = "refreshed"
    with pytest.raises(RuntimeError, match="changed during"):
        auth._accept_refresh(json.dumps(data))
    assert source.read_text() == "new user login"
