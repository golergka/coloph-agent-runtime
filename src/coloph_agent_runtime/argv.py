from __future__ import annotations

import json
from typing import Any


def build_claude_argv(
    prompt: str,
    *,
    allowed_tools: list[str],
    permission_mode: str | None,
    session_id: str | None = None,
    resume: bool = True,
    max_budget_usd: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    json_schema: dict[str, Any] | None = None,
) -> list[str]:
    """Build argv for a headless claude invocation."""
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if permission_mode:
        argv.extend(["--permission-mode", permission_mode])
    argv.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools or not allowed_tools:
        argv.extend(["--disallowed-tools", ",".join(disallowed_tools or ["Agent"])])
    if session_id:
        if resume:
            argv.extend(["--resume", session_id])
        else:
            argv.extend(["--session-id", session_id])
    if max_turns is not None:
        argv.extend(["--max-turns", str(max_turns)])
    if max_budget_usd:
        argv.extend(["--max-budget-usd", str(max_budget_usd)])
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    if json_schema is not None:
        argv.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"), sort_keys=True)])
    return argv
