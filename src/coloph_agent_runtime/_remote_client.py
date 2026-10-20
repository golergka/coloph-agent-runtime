"""Claude client over a duplex JSON stream. No Kubernetes dependencies."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from ._remote_protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, RemoteTransportError, decode_error, encode_auth
from .auth import Auth, CLIAuth
from .runner import ClaudeCancelledError
from .stream import EventCallback
from .types import ClaudeResult, RunOptions


class Channel(Protocol):
    def write_stdin(self, data: str) -> None: ...
    def update(self, timeout: float) -> None: ...
    def read_stdout(self) -> str: ...
    def is_open(self) -> bool: ...
    def close(self) -> None: ...


class RemoteClaudeClient:
    """One invocation per call, sharing the container's filesystem and environment."""

    def __init__(self, connect: Callable[[], Channel], *, executable: str = "claude", auth: Auth | None = None) -> None:
        self._connect = connect
        self.executable = executable
        self.auth = auth if auth is not None else CLIAuth()

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        options: RunOptions,
        session_id: str | None = None,
        resume: bool = True,
        env_overrides: Mapping[str, str] | None = None,
        base_env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
        on_event: EventCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ClaudeResult:
        if cancel is not None and cancel.is_set():
            raise ClaudeCancelledError("Claude Code invocation was cancelled before launch")
        request = {
            "version": PROTOCOL_VERSION,
            "op": "run",
            "prompt": prompt,
            "cwd": str(cwd),
            "options": asdict(options),
            "executable": self.executable,
            "auth": encode_auth(self.auth),
            "session_id": session_id,
            "resume": resume,
            "stdin_text": stdin_text,
            "env_overrides": dict(env_overrides) if env_overrides is not None else None,
            "base_env": dict(base_env) if base_env is not None else None,
        }
        return ClaudeResult(**exchange(self._connect, request, on_event=on_event, cancel=cancel))


def exchange(
    connect: Callable[[], Channel],
    request: dict[str, Any],
    *,
    on_event: EventCallback | None,
    cancel: threading.Event | None,
    on_control: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    payload = json.dumps(request) + "\n"
    if len(payload.encode()) > MAX_FRAME_BYTES:
        raise ValueError("Request exceeds protocol frame limit")
    frames: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
    stop = threading.Event()
    abort = threading.Event()

    def communicate() -> None:
        channel: Channel | None = None
        try:
            channel = connect()
            channel.write_stdin(payload)
            pending = ""
            last_ping = 0.0
            cancel_at: float | None = None
            while not stop.is_set():
                now = time.monotonic()
                if abort.is_set() or (cancel is not None and cancel.is_set()):
                    if cancel_at is None:
                        channel.write_stdin('{"op":"cancel"}\n')
                        cancel_at = now
                    elif now - cancel_at > 15:
                        raise RemoteTransportError("Worker did not acknowledge cancellation; outcome is uncertain")
                if now - last_ping > 1:
                    channel.write_stdin('{"op":"ping"}\n')
                    last_ping = now
                channel.update(timeout=0.1)
                pending += channel.read_stdout()
                if len(pending.encode()) > MAX_FRAME_BYTES:
                    raise RemoteTransportError("Worker response exceeds protocol frame limit")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    frame = json.loads(line)
                    if not isinstance(frame, dict) or frame.get("type") not in {
                        "event",
                        "result",
                        "error",
                        "credentials",
                    }:
                        raise RemoteTransportError("Invalid worker response")
                    frames.put(frame)
                    if frame["type"] in {"result", "error"}:
                        return
                if not channel.is_open():
                    raise RemoteTransportError("Worker stream closed without a terminal result; outcome is uncertain")
        except Exception as exc:
            # Transport boundary reports errors to the caller thread.
            frames.put(exc if isinstance(exc, RemoteTransportError) else RemoteTransportError(str(exc)))
        finally:
            if channel is not None:
                channel.close()

    io = threading.Thread(target=communicate, daemon=True)
    io.start()
    try:
        while True:
            frame = frames.get()
            if isinstance(frame, Exception):
                raise frame
            if frame["type"] == "event":
                if on_event is not None:
                    on_event(frame["event"])
            elif frame["type"] == "credentials":
                if on_control is not None:
                    on_control(frame)
            elif frame["type"] == "error":
                raise decode_error(frame["error"])
            else:
                result = frame["result"]
                if not isinstance(result, dict):
                    raise RemoteTransportError("Worker result must be an object")
                return result
    finally:
        # Callback exceptions and KeyboardInterrupt also cancel the remote process.
        abort.set()
        io.join(timeout=16)
        stop.set()
        io.join(timeout=2)
        if io.is_alive():
            raise RemoteTransportError("Transport did not stop; remote cleanup could not be confirmed")
        # A callback can fail before the worker returns a refreshed login cache.
        while not frames.empty():
            remaining = frames.get_nowait()
            if isinstance(remaining, dict) and remaining.get("type") == "credentials" and on_control is not None:
                on_control(remaining)


def hello(connect: Callable[[], Channel]) -> None:
    channel = connect()
    try:
        channel.write_stdin(json.dumps({"op": "hello", "version": PROTOCOL_VERSION}) + "\n")
        deadline = time.monotonic() + 10
        pending = ""
        while time.monotonic() < deadline:
            channel.update(timeout=0.1)
            pending += channel.read_stdout()
            if "\n" in pending:
                frame = json.loads(pending.split("\n", 1)[0])
                if frame != {"type": "hello", "version": PROTOCOL_VERSION}:
                    raise RemoteTransportError("Worker protocol handshake failed")
                return
            if not channel.is_open():
                break
        raise RemoteTransportError("Worker protocol handshake did not complete")
    finally:
        channel.close()
