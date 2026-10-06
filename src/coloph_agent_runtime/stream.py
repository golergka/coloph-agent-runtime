from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TextIO

from coloph_agent_runtime.types import ClaudeResult

Event = dict[str, Any]
EventCallback = Callable[[Event], None]


class JsonlRecorder:
    """Write events to a caller-owned stream. The caller opens and closes it."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def __call__(self, event: Event) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.stream.flush()


def result_from_event(event: Event, *, events: list[Event], stderr: str) -> ClaudeResult:
    usage = event.get("usage") or {}
    return ClaudeResult(
        session_id=event["session_id"],
        response_text=event.get("result"),
        structured_output=event.get("structured_output"),
        model=event.get("model"),
        cost_usd=event.get("total_cost_usd"),
        num_turns=event.get("num_turns"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_tokens=usage.get("cache_creation_input_tokens"),
        stop_reason=event.get("stop_reason"),
        duration_ms=event.get("duration_ms"),
        api_duration_ms=event.get("duration_api_ms"),
        stderr=stderr,
        events=events,
        terminal_event=event,
    )
