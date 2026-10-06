from __future__ import annotations

import re
import subprocess

MINIMUM_CLAUDE_CODE_VERSION = (2, 1, 215)


class ClaudeCodeVersionError(RuntimeError):
    """The executable is missing, unreadable, or unsupported."""


def require_supported_claude_code_version(
    executable: str = "claude",
    *,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ClaudeCodeVersionError("Claude Code executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeVersionError("Claude Code version check timed out") from exc
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout + result.stderr)
    if result.returncode or match is None:
        raise ClaudeCodeVersionError("Cannot determine the Claude Code version")
    version = (int(match[1]), int(match[2]), int(match[3]))
    if version < MINIMUM_CLAUDE_CODE_VERSION:
        raise ClaudeCodeVersionError("Claude Code 2.1.215 or later is required")
    return version
