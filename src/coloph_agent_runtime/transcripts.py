from __future__ import annotations

import json
import re
from pathlib import Path

from coloph_agent_runtime._diagnostics import report_exception
from coloph_agent_runtime._messages import message as _msg


def claude_projects_dir_for_cwd(cwd: Path) -> Path:
    """Resolve Claude Code's transcript directory for a working directory."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return Path.home() / ".claude" / "projects" / slug


def is_empty_thinking_block(block: object) -> bool:
    return isinstance(block, dict) and block.get("type") == "thinking" and not block.get("thinking")


def repair_transcript_file(transcript: Path) -> int:
    """Strip corrupted standalone empty-thinking lines from a Claude transcript."""
    lines = transcript.read_text().splitlines()
    kept: list[str] = []
    removed = 0
    for line_no, raw in enumerate(lines):
        if not raw.strip():
            kept.append(raw)
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:  # EXPECTED_EXCEPTION - corrupted transcript may contain non-JSON lines.
            report_exception(exc, operation="claude_process.transcript.repair_parse_line")
            kept.append(raw)
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            kept.append(raw)
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            kept.append(raw)
            continue
        if not any(is_empty_thinking_block(block) for block in content):
            kept.append(raw)
            continue
        if len(content) == 1:
            removed += 1
            continue
        block_types = [block.get("type") for block in content if isinstance(block, dict)]
        raise ValueError(
            _msg(
                "claude_transcript_empty_thinking_mixed_content",
                transcript=transcript,
                line=line_no,
                block_types=block_types,
            )
        )
    if removed:
        tmp = transcript.with_name(transcript.name + ".repair-tmp")
        tmp.write_text("".join(line + "\n" for line in kept))
        tmp.replace(transcript)
    return removed


def repair_resume_transcript(session_id: str, cwd: Path) -> int:
    """Repair a session transcript in place before a --resume replay."""
    transcript = claude_projects_dir_for_cwd(cwd) / f"{session_id}.jsonl"
    if not transcript.exists():
        return 0
    return repair_transcript_file(transcript)
