"""One headless Codex invocation, with the same call shape as ClaudeClient."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .codex_auth import CodexSubscriptionAuth
from .errors import ClaudeInvocationError
from .runner import ClaudeCancelledError, ClaudeProtocolError, ClaudeResultError, run_process
from .stream import EventCallback


@dataclass(frozen=True)
class CodexRunOptions:
    model: str | None = None
    effort: str | None = None
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    timeout_seconds: float = 7200.0
    json_schema: dict[str, Any] | None = None
    capture_events: bool = True
    skip_git_repo_check: bool = False
    ignore_user_config: bool = False
    ephemeral: bool = False


@dataclass
class CodexResult:
    session_id: str
    response_text: str | None = None
    structured_output: object | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    stderr: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    terminal_event: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CodexCLIAuth:
    """Use the saved login in the execution environment, optionally a specific home."""

    codex_home: str | None = None
    credential_store: Literal["file", "keyring", "auto"] | None = None


class CodexInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        events: list[dict[str, Any]] | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr, self.events, self.returncode = stderr, events or [], returncode


class CodexProtocolError(CodexInvocationError):
    pass


class CodexCancelledError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, *, executable: str = "codex", auth: CodexCLIAuth | CodexSubscriptionAuth | None = None) -> None:
        self.executable = executable
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
        if isinstance(self.auth, CodexSubscriptionAuth):
            import fcntl
            import hashlib

            self.auth._validate()
            account = json.loads(self.auth._auth_json)["tokens"].get("account_id")
            if not account:
                raise ValueError("The subscription login cache has no account ID")
            home = (
                Path.home()
                / ".local/share/coloph-agent-runtime/codex"
                / hashlib.sha256(account.encode()).hexdigest()[:24]
            )
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (home / ".lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError("This subscription cache already has an active invocation") from exc
                self.auth.save(home / "auth.json")
                try:
                    return CodexClient(executable=self.executable, auth=CodexCLIAuth(str(home), "file")).run(
                        prompt,
                        cwd=cwd,
                        options=options,
                        session_id=session_id,
                        resume=resume,
                        env_overrides=env_overrides,
                        base_env=base_env,
                        stdin_text=stdin_text,
                        on_event=on_event,
                        cancel=cancel,
                    )
                finally:
                    self.auth._accept_refresh((home / "auth.json").read_text())
        if cancel is not None and cancel.is_set():
            raise CodexCancelledError("Codex invocation was cancelled before launch")
        if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if session_id == "" or (session_id is not None and not resume):
            raise ValueError("Codex assigns new session IDs; supply an ID only to resume")
        if session_id is not None and options.ephemeral:
            raise ValueError("Ephemeral invocations cannot resume sessions")
        if options.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("Unsupported Codex sandbox")
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError(str(cwd))
        env = dict(os.environ if base_env is None else base_env)
        credentials = {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"}
        if env_overrides and credentials.intersection(env_overrides):
            raise ValueError("Saved-login authentication cannot accept API credentials through env_overrides")
        for key in credentials:
            env.pop(key, None)
        env.update(env_overrides or {})
        if self.auth.codex_home is not None:
            env["CODEX_HOME"] = self.auth.codex_home
        executable = shutil.which(self.executable, path=env.get("PATH"))
        if executable is None:
            raise FileNotFoundError(self.executable)
        argv = [
            executable,
            "exec",
            "--json",
            "-c",
            'approval_policy="never"',
            "-c",
            f"sandbox_mode={json.dumps(options.sandbox)}",
        ]
        if session_id is not None:
            argv.extend(["resume", session_id])
        if self.auth.credential_store is not None:
            argv.extend(["-c", "cli_auth_credentials_store=" + json.dumps(self.auth.credential_store)])
        if options.model:
            argv.extend(["--model", options.model])
        if options.effort:
            argv.extend(["-c", "model_reasoning_effort=" + json.dumps(options.effort)])
        for flag, enabled in (
            ("--skip-git-repo-check", options.skip_git_repo_check),
            ("--ignore-user-config", options.ignore_user_config),
            ("--ephemeral", options.ephemeral),
        ):
            if enabled:
                argv.append(flag)
        state: dict[str, Any] = {"session": None, "text": None, "terminal": {}}
        observed: list[dict[str, Any]] = []

        def adapt(event: dict[str, Any]) -> dict[str, Any]:
            if options.capture_events:
                observed.append(event)
            kind = event.get("type")
            if kind == "thread.started":
                state["session"] = event.get("thread_id")
            if kind == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    state["text"] = item.get("text")
            if kind in {"turn.completed", "turn.failed"}:
                state["terminal"] = event
                usage = event.get("usage") or {}
                return {
                    "type": "result",
                    "subtype": "success" if kind == "turn.completed" else "error",
                    "is_error": kind == "turn.failed",
                    "session_id": state["session"],
                    "result": state["text"],
                    "usage": usage,
                }
            return {"type": "codex_event"}

        with tempfile.TemporaryDirectory(prefix="codex-schema-") as directory:
            if options.json_schema is not None:
                schema = Path(directory) / "schema.json"
                schema.write_text(json.dumps(options.json_schema))
                argv.extend(["--output-schema", str(schema)])
            argv.append("-")
            input_text = prompt if stdin_text is None else prompt + "\n<stdin>\n" + stdin_text + "\n</stdin>"
            try:
                result = run_process(
                    argv,
                    cwd=cwd,
                    env=env,
                    deadline=time.monotonic() + options.timeout_seconds,
                    stdin_text=input_text,
                    on_event=on_event,
                    capture_events=options.capture_events,
                    cancel=cancel,
                    event_adapter=adapt,
                )
            except ClaudeCancelledError as exc:
                raise CodexCancelledError("Codex invocation was cancelled") from exc
            except ClaudeProtocolError as exc:
                raise CodexProtocolError(str(exc), stderr=exc.stderr, events=exc.events) from exc
            except (ClaudeInvocationError, ClaudeResultError) as exc:
                error = state["terminal"].get("error", {})
                raise CodexInvocationError(
                    error.get("message") or "Codex invocation failed",
                    stderr=exc.stderr or "",
                    returncode=exc.returncode,
                    events=observed,
                ) from exc
        structured = None
        if options.json_schema is not None:
            try:
                structured = json.loads(result.response_text or "")
            except json.JSONDecodeError as exc:
                raise CodexProtocolError(
                    "Codex did not return valid structured JSON", stderr=result.stderr, events=result.events
                ) from exc
        return CodexResult(
            session_id=result.session_id,
            response_text=result.response_text,
            structured_output=structured,
            model=options.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=state["terminal"].get("usage", {}).get("cached_input_tokens"),
            stderr=result.stderr,
            events=result.events,
            terminal_event=state["terminal"],
        )
