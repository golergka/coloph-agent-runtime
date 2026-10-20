from __future__ import annotations

import errno
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from coloph_agent_runtime.errors import ClaudeInvocationError, classify_synthetic_error
from coloph_agent_runtime.stream import Event, EventCallback, result_from_event
from coloph_agent_runtime.types import ClaudeResult

logger = logging.getLogger(__name__)


class ClaudeProtocolError(RuntimeError):
    """The CLI stream did not contain a valid terminal result."""

    def __init__(self, message: str, *, events: list[Event], stderr: str) -> None:
        super().__init__(message)
        self.events = events
        self.stderr = stderr


class ClaudeCancelledError(RuntimeError):
    """The caller cancelled the attempt. Its process has been reaped."""


class ClaudeResultError(ClaudeInvocationError):
    """The CLI reported an unsuccessful terminal result."""

    def __init__(self, argv: list[str], *, event: Event, stderr: str) -> None:
        super().__init__(1, argv, stderr=stderr, synthetic_texts=[str(event.get("result") or event.get("subtype"))])
        self.terminal_event = event


def _signal_process(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif proc.poll() is None:
            proc.kill()
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    stdin_text: str | None,
    on_event: EventCallback | None,
    capture_events: bool,
    cancel: threading.Event | None,
    gateway_diagnostics_path: Path | None = None,
    event_adapter: Callable[[Event], Event] | None = None,
) -> ClaudeResult:
    if cancel is not None and cancel.is_set():
        raise ClaudeCancelledError("Claude Code invocation was cancelled before launch")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(argv, 0)
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    lines: queue.Queue[str | None | OSError] = queue.Queue()
    stderr_parts: list[str] = []
    io_failures: list[OSError] = []
    workers: list[threading.Thread] = []
    events: list[Event] = []
    terminal: Event | None = None
    synthetic_texts: list[str] = []
    reset_at = None
    timed_out = threading.Event()
    cancelled = threading.Event()
    stopped = threading.Event()

    def read_stdout(stream: TextIO) -> None:
        try:
            for line in stream:
                lines.put(line)
        except OSError as exc:
            lines.put(exc)
        finally:
            lines.put(None)

    def read_stderr(stream: TextIO) -> None:
        try:
            stderr_parts.append(stream.read())
        except OSError as exc:
            io_failures.append(exc)

    def write_stdin(stream: TextIO, text: str) -> None:
        try:
            stream.write(text)
            stream.close()
        except BrokenPipeError:
            return
        except OSError as exc:
            io_failures.append(exc)

    def supervise() -> None:
        while not stopped.wait(0.025):
            if cancel is not None and cancel.is_set():
                cancelled.set()
                _signal_process(proc, signal.SIGINT)
                if not stopped.wait(min(1.0, max(0.0, deadline - time.monotonic()))):
                    _signal_process(proc, signal.SIGKILL)
                return
            if time.monotonic() >= deadline:
                timed_out.set()
                _signal_process(proc, signal.SIGKILL)
                return

    def start_worker(target: Callable[..., None], *args: Any) -> None:
        worker = threading.Thread(target=target, args=args, daemon=True)
        workers.append(worker)
        worker.start()

    try:
        assert proc.stdout is not None and proc.stderr is not None
        start_worker(read_stdout, proc.stdout)
        start_worker(read_stderr, proc.stderr)
        start_worker(supervise)
        if stdin_text is not None:
            assert proc.stdin is not None
            start_worker(write_stdin, proc.stdin, stdin_text)
        while True:
            if gateway_diagnostics_path is not None and gateway_diagnostics_path.exists():
                diagnostics = gateway_diagnostics_path.read_text(encoding="utf-8")
                if diagnostics:
                    raise classify_synthetic_error(
                        1,
                        argv,
                        stderr="".join(stderr_parts),
                        log_tail=diagnostics,
                        synthetic_texts=["Claude gateway diagnostics: " + diagnostics],
                    )
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                break
            if isinstance(line, OSError):
                raise line
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Ignoring a non-JSON stdout line")
                continue
            if not isinstance(event, dict):
                continue
            if capture_events:
                events.append(event)
            if on_event is not None:
                on_event(event)
            if event_adapter is not None:
                event = event_adapter(event)
            if event.get("type") == "assistant":
                message = event.get("message") or {}
                if isinstance(message, dict) and message.get("model") == "<synthetic>":
                    synthetic_texts.extend(
                        str(block.get("text", ""))
                        for block in message.get("content", [])
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
            if event.get("type") == "rate_limit_event":
                from datetime import datetime, timezone

                rate_limit_info = event.get("rate_limit_info")
                value = rate_limit_info.get("resetsAt") if isinstance(rate_limit_info, dict) else None
                try:
                    if value is not None:
                        reset_at = datetime.fromtimestamp(float(value), timezone.utc)
                except (ValueError, TypeError, OverflowError, OSError):
                    logger.debug("Ignoring an invalid rate-limit reset timestamp")
            if event.get("type") == "result":
                if terminal is not None:
                    raise ClaudeProtocolError("Multiple terminal results", events=events, stderr="".join(stderr_parts))
                terminal = event
        # The supervisor remains active until both pipe reading and process exit finish.
        proc.wait(timeout=max(0.1, deadline - time.monotonic() + 0.1))
    finally:
        stopped.set()
        _signal_process(proc, signal.SIGKILL)
        proc.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BrokenPipeError:
                    # The child can exit before consuming all caller-provided stdin.
                    pass
    stderr = "".join(stderr_parts)
    if cancelled.is_set():
        raise ClaudeCancelledError("Claude Code invocation was cancelled")
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(argv, remaining, stderr=stderr)
    if io_failures:
        raise io_failures[0]
    if proc.returncode:
        raise classify_synthetic_error(
            proc.returncode,
            argv,
            stderr=stderr,
            synthetic_texts=synthetic_texts,
            rate_limit_reset_at=reset_at,
            log_tail="",
        )
    if terminal is None or not isinstance(terminal.get("session_id"), str) or not terminal["session_id"]:
        raise ClaudeProtocolError("Missing terminal result or session ID", events=events, stderr=stderr)
    if terminal.get("is_error") or terminal.get("subtype") != "success":
        raise ClaudeResultError(argv, event=terminal, stderr=stderr)
    usage = terminal.get("usage") or {}
    if not isinstance(usage, dict):
        raise ClaudeProtocolError("Terminal usage must be an object", events=events, stderr=stderr)
    if terminal.get("result") == "" and usage.get("input_tokens") == 0 and usage.get("output_tokens") == 0:
        if terminal.get("structured_output") is None:
            raise ClaudeProtocolError("Empty zero-token result", events=events, stderr=stderr)
    return result_from_event(terminal, events=events, stderr=stderr)
