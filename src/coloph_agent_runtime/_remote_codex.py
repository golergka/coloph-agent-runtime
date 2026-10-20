from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from ._remote_client import Channel, exchange
from ._remote_protocol import PROTOCOL_VERSION
from .codex import CodexCancelledError, CodexCLIAuth, CodexResult, CodexRunOptions
from .codex_auth import CodexSubscriptionAuth
from .stream import EventCallback


class RemoteCodexClient:
    def __init__(
        self,
        connect: Callable[[], Channel],
        *,
        executable: str = "codex",
        auth: CodexCLIAuth | CodexSubscriptionAuth | None = None,
    ) -> None:
        self._connect, self.executable = connect, executable
        self.auth = auth if auth is not None else CodexCLIAuth()

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        options: CodexRunOptions,
        session_id: str | None = None,
        resume: bool = True,
        env_overrides: Mapping[str, str] | None = None,
        base_env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
        on_event: EventCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> CodexResult:
        if cancel is not None and cancel.is_set():
            raise CodexCancelledError("Codex invocation was cancelled before launch")
        request = {
            "version": PROTOCOL_VERSION,
            "op": "run",
            "agent": "codex",
            "prompt": prompt,
            "cwd": str(cwd),
            "options": asdict(options),
            "executable": self.executable,
            "auth": (
                {"subscription": self.auth._auth_json}
                if isinstance(self.auth, CodexSubscriptionAuth)
                else asdict(self.auth)
            ),
            "session_id": session_id,
            "resume": resume,
            "stdin_text": stdin_text,
            "env_overrides": dict(env_overrides) if env_overrides is not None else None,
            "base_env": dict(base_env) if base_env is not None else None,
        }

        def credentials(frame: dict[str, object]) -> None:
            if isinstance(self.auth, CodexSubscriptionAuth):
                value = frame["auth_json"]
                if not isinstance(value, str):
                    raise ValueError("Invalid subscription credential update")
                self.auth._accept_refresh(value)

        if isinstance(self.auth, CodexSubscriptionAuth):
            if not self.auth._lock.acquire(blocking=False):
                raise ValueError("This subscription auth object already has an active invocation")
            try:
                return CodexResult(
                    **exchange(self._connect, request, on_event=on_event, cancel=cancel, on_control=credentials)
                )
            finally:
                self.auth._lock.release()
        return CodexResult(**exchange(self._connect, request, on_event=on_event, cancel=cancel))
