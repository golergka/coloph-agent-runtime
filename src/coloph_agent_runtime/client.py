from __future__ import annotations

import math
import os
import shutil
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from coloph_agent_runtime.argv import build_claude_argv
from coloph_agent_runtime.auth import AnthropicAuth, Auth, CLIAuth, OAuthAuth, OpenAIAuth, build_environment
from coloph_agent_runtime.runner import ClaudeCancelledError, run_process
from coloph_agent_runtime.stream import EventCallback
from coloph_agent_runtime.transcripts import repair_resume_transcript
from coloph_agent_runtime.types import ClaudeResult, RunOptions
from coloph_agent_runtime.version import require_supported_claude_code_version


class ClaudeClient:
    """Run one synchronous attempt. Calls do not retain conversation state."""

    def __init__(self, *, executable: str = "claude", auth: Auth | None = None) -> None:
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
        if os.name != "posix":
            raise NotImplementedError("coloph-agent-runtime 0.1 supports POSIX process ownership only")
        if cancel is not None and cancel.is_set():
            raise ClaudeCancelledError("Claude Code invocation was cancelled before launch")
        if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if options.max_budget_usd is not None and (
            not math.isfinite(options.max_budget_usd) or options.max_budget_usd <= 0
        ):
            raise ValueError("max_budget_usd must be finite and positive")
        if options.max_turns is not None and options.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if session_id == "":
            raise ValueError("session_id must not be empty")
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory")
        base = dict(os.environ if base_env is None else base_env)
        context_env = build_environment(CLIAuth(), base, env_overrides)
        # Resolve once so preflight and execution always use the same executable.
        executable = shutil.which(self.executable, path=context_env.get("PATH"))
        if executable is None:
            raise FileNotFoundError(self.executable)
        executable = str(Path(executable).resolve())
        deadline = time.monotonic() + options.timeout_seconds
        sid = session_id if session_id is not None else str(uuid4())
        resume = resume and session_id is not None
        if resume and options.repair_transcript:
            repair_resume_transcript(sid, cwd)

        def execute(auth: CLIAuth | OAuthAuth | AnthropicAuth, diagnostics: Path | None = None) -> ClaudeResult:
            env = build_environment(auth, base, env_overrides)
            require_supported_claude_code_version(
                executable,
                env=env,
                timeout=max(0.001, min(10.0, deadline - time.monotonic())),
            )
            argv = build_claude_argv(
                prompt,
                allowed_tools=list(options.allowed_tools),
                disallowed_tools=list(options.disallowed_tools),
                permission_mode=options.permission_mode,
                session_id=sid,
                resume=resume,
                max_budget_usd=options.max_budget_usd,
                model=options.model,
                effort=options.effort,
                max_turns=options.max_turns,
                json_schema=options.json_schema,
            )
            argv[0] = executable
            return run_process(
                argv,
                cwd=cwd,
                env=env,
                deadline=deadline,
                stdin_text=stdin_text,
                on_event=on_event,
                capture_events=options.capture_events,
                cancel=cancel,
                gateway_diagnostics_path=diagnostics,
            )

        if isinstance(self.auth, OpenAIAuth):
            from coloph_agent_runtime.gateway import start_litellm_gateway

            options = replace(options, model=options.model or self.auth.model)
            with start_litellm_gateway(
                provider=self.auth,
                readiness_timeout_seconds=max(0.001, min(30.0, deadline - time.monotonic())),
                cancel=cancel,
            ) as gateway:
                return execute(
                    AnthropicAuth(base_url=gateway.base_url, auth_token=gateway.auth_token),
                    gateway.diagnostics_path,
                )
        return execute(self.auth)
