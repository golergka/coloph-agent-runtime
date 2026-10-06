from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunOptions:
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: str | None = None
    model: str | None = None
    effort: str | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    timeout_seconds: float = 7200.0
    json_schema: dict[str, Any] | None = None
    capture_events: bool = True
    repair_transcript: bool = False


@dataclass
class ClaudeResult:
    session_id: str
    response_text: str | None = None
    structured_output: object | None = None
    model: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    stop_reason: str | None = None
    duration_ms: int | None = None
    api_duration_ms: int | None = None
    stderr: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    terminal_event: dict[str, Any] = field(default_factory=dict)
