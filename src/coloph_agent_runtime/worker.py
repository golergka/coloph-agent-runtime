"""Container entrypoint and Kubernetes exec bridge. No optional dependencies."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ._remote_protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, WorkerBusyError, decode_auth, encode_error
from .client import ClaudeClient
from .types import RunOptions

DEFAULT_SOCKET = "/tmp/coloph-agent-runtime.sock"
LEASE_SECONDS = 10


class WorkerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False

    def __init__(self, path: str) -> None:
        self.invocation_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(path, WorkerHandler)
        os.chmod(path, 0o600)


class WorkerHandler(socketserver.StreamRequestHandler):
    server: WorkerServer

    def handle(self) -> None:
        self.connection.settimeout(15)
        try:
            raw = self.rfile.readline(MAX_FRAME_BYTES + 1)
            if len(raw) > MAX_FRAME_BYTES:
                raise ValueError("Request exceeds protocol frame limit")
            request = json.loads(raw)
            if request.get("version") != PROTOCOL_VERSION:
                raise ValueError("Incompatible worker protocol version")
            if request.get("op") == "hello":
                self.send({"type": "hello", "version": PROTOCOL_VERSION})
                return
            if request.get("op") != "run":
                raise ValueError("Unknown worker operation")
            if not self.server.invocation_lock.acquire(blocking=False):
                raise WorkerBusyError("The worker already has an active invocation")
            try:
                result = self.execute(request)
            finally:
                self.server.invocation_lock.release()
            self.send({"type": "result", "result": result})
        except Exception as exc:
            # Protocol boundary: serialize failures for the caller, never retry.
            try:
                self.send({"type": "error", "error": encode_error(exc)})
            except OSError:
                # The caller disconnected; execute() has already reaped the agent.
                return

    def send(self, frame: dict[str, Any]) -> None:
        data = (json.dumps(frame) + "\n").encode()
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("Response exceeds protocol frame limit")
        self.wfile.write(data)
        self.wfile.flush()

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        cancel = threading.Event()
        finished = threading.Event()

        def monitor() -> None:
            last_ping = time.monotonic()
            while not finished.is_set():
                if self.server.stopping.is_set() or time.monotonic() - last_ping > LEASE_SECONDS:
                    cancel.set()
                    return
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0.2)
                    if not ready:
                        continue
                    line = self.rfile.readline(1024)
                    if not line:
                        cancel.set()
                        return
                    message = json.loads(line)
                    if message.get("op") == "cancel":
                        cancel.set()
                    elif message.get("op") == "ping":
                        last_ping = time.monotonic()
                except (OSError, ValueError):
                    cancel.set()
                    return

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        try:
            options = dict(request["options"])
            options["allowed_tools"] = tuple(options["allowed_tools"])
            options["disallowed_tools"] = tuple(options["disallowed_tools"])
            result = ClaudeClient(executable=request["executable"], auth=decode_auth(request["auth"])).run(
                request["prompt"],
                cwd=Path(request["cwd"]),
                options=RunOptions(**options),
                session_id=request["session_id"],
                resume=request["resume"],
                env_overrides=request["env_overrides"],
                base_env=request["base_env"],
                stdin_text=request["stdin_text"],
                cancel=cancel,
                on_event=lambda event: self.send({"type": "event", "event": event}),
            )
            return asdict(result)
        finally:
            finished.set()
            observer.join(timeout=1)


def bridge(path: str) -> None:
    """Forward stdin/stdout only; credentials never enter command arguments."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(path)
        while True:
            readable, _, _ = select.select([connection, sys.stdin.buffer], [], [])
            if connection in readable:
                data = connection.recv(65536)
                if not data:
                    return
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            if sys.stdin.buffer in readable:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    return
                connection.sendall(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("serve", "bridge"), default="serve")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args()
    if args.mode == "bridge":
        bridge(args.socket)
        return
    with WorkerServer(args.socket) as server:

        def stop(signum: int, frame: object) -> None:
            server.stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.stopping.set()
    Path(args.socket).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
