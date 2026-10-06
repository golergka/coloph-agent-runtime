from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time

import pytest

from coloph_agent_runtime import (
    ClaudeCancelledError,
    ClaudeClient,
    ClaudeProtocolError,
    ClaudeResultError,
    ClaudeUsageLimitError,
    JsonlRecorder,
    OAuthAuth,
    RunOptions,
)


def options(**kwargs):
    return RunOptions(allowed_tools=("Read",), **kwargs)


def test_session_create_resume_and_event_recording(fake_claude, tmp_path):
    client = ClaudeClient(executable=str(fake_claude))
    log = io.StringIO()
    result = client.run(
        "structured", cwd=tmp_path, options=options(json_schema={"type": "object"}), on_event=JsonlRecorder(log)
    )
    assert result.structured_output == {"answer": 42}
    assert result.response_text == "done"
    assert result.cost_usd == 0.01
    assert result.input_tokens == 4
    assert [json.loads(line) for line in log.getvalue().splitlines()] == result.events
    assert not log.closed
    client.run("ok", cwd=tmp_path, options=options(), session_id=result.session_id)
    assert "--resume" in json.loads((tmp_path / "argv.json").read_text())
    client.run("ok", cwd=tmp_path, options=options(), session_id=result.session_id, resume=False)
    assert "--session-id" in json.loads((tmp_path / "argv.json").read_text())


def test_environment_boundary(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-secret")
    result = ClaudeClient(executable=str(fake_claude), auth=OAuthAuth("explicit-token")).run(
        "ok",
        cwd=tmp_path,
        options=options(capture_events=False),
        env_overrides={"APP_ROLE": "worker"},
    )
    assert result.events == []
    assert json.loads((tmp_path / "context.json").read_text()) == {
        "APP_ROLE": "worker",
        "ANTHROPIC_API_KEY": None,
        "CLAUDE_CODE_OAUTH_TOKEN": "explicit-token",
    }


@pytest.mark.parametrize("mode", ["missing", "empty", "duplicate", "bad_usage"])
def test_protocol_failure(fake_claude, tmp_path, mode):
    with pytest.raises(ClaudeProtocolError):
        ClaudeClient(executable=str(fake_claude)).run(mode, cwd=tmp_path, options=options())


def test_terminal_failure(fake_claude, tmp_path):
    with pytest.raises(ClaudeResultError) as failure:
        ClaudeClient(executable=str(fake_claude)).run("terminal_error", cwd=tmp_path, options=options())
    assert failure.value.terminal_event["subtype"] == "error_max_turns"


def test_usage_limit_preserves_reset_and_stderr(fake_claude, tmp_path):
    with pytest.raises(ClaudeUsageLimitError) as failure:
        ClaudeClient(executable=str(fake_claude)).run("failure", cwd=tmp_path, options=options())
    assert failure.value.rate_limit_reset_at.timestamp() == 1900000000
    assert "diagnostic" in failure.value.stderr


def assert_reaped(tmp_path):
    pid = int((tmp_path / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_reaps_child(fake_claude, tmp_path):
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ClaudeClient(executable=str(fake_claude)).run("sleep", cwd=tmp_path, options=options(timeout_seconds=0.5))
    assert time.monotonic() - start < 5
    assert_reaped(tmp_path)


def test_cancellation_reaps_child(fake_claude, tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        with pytest.raises(ClaudeCancelledError):
            ClaudeClient(executable=str(fake_claude)).run("sleep", cwd=tmp_path, options=options(), cancel=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert_reaped(tmp_path)


def test_callback_failure_cleans_up(fake_claude, tmp_path):
    def fail(event):
        raise ValueError("storage failed")

    with pytest.raises(ValueError, match="storage failed"):
        ClaudeClient(executable=str(fake_claude)).run("callback", cwd=tmp_path, options=options(), on_event=fail)
    assert_reaped(tmp_path)


def test_large_stdin_and_stderr_do_not_deadlock(fake_claude, tmp_path):
    result = ClaudeClient(executable=str(fake_claude)).run(
        "pipes",
        cwd=tmp_path,
        options=options(timeout_seconds=5),
        stdin_text="x" * 200000,
    )
    assert len(result.stderr) == 200000
    assert (tmp_path / "input_size").read_text() == "200000"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_fails_before_launch(fake_claude, tmp_path, timeout):
    with pytest.raises(ValueError):
        ClaudeClient(executable=str(fake_claude)).run("ok", cwd=tmp_path, options=options(timeout_seconds=timeout))
    assert not (tmp_path / "child.pid").exists()


def test_timeout_stops_tool_descendants(fake_claude, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        ClaudeClient(executable=str(fake_claude)).run("descendant", cwd=tmp_path, options=options(timeout_seconds=0.5))
    assert_reaped(tmp_path)
    time.sleep(1.6)
    assert not (tmp_path / "survivor").exists()


def test_cancellation_before_launch(fake_claude, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ClaudeCancelledError):
        ClaudeClient(executable=str(fake_claude)).run("ok", cwd=tmp_path, options=options(), cancel=cancel)
    assert not (tmp_path / "child.pid").exists()


def test_early_exit_during_stdin_preserves_protocol_error(fake_claude, tmp_path):
    with pytest.raises(ClaudeProtocolError):
        ClaudeClient(executable=str(fake_claude)).run(
            "missing",
            cwd=tmp_path,
            options=options(),
            stdin_text="x" * 200000,
        )


def test_concurrent_calls_do_not_replace_signal_handlers(fake_claude, tmp_path):
    import signal
    from concurrent.futures import ThreadPoolExecutor

    before = signal.getsignal(signal.SIGTERM)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    client = ClaudeClient(executable=str(fake_claude))
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(client.run, "ok", cwd=cwd, options=options()) for cwd in (first, second)]
        results = [job.result(timeout=10) for job in jobs]
    assert results[0].session_id != results[1].session_id
    assert signal.getsignal(signal.SIGTERM) == before
