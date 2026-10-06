from __future__ import annotations

import calendar
import re
import subprocess
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as datetime_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coloph_agent_runtime._messages import message as _msg
from coloph_agent_runtime._messages import messages as _ui


class ClaudeInvocationError(subprocess.CalledProcessError):
    """A claude subprocess exited non-zero with parsed diagnostics."""

    def __init__(
        self,
        returncode: int,
        cmd: list[str],
        *,
        stderr: str | None = None,
        synthetic_texts: list[str] | None = None,
        log_tail: str = "",
        rate_limit_reset_at: datetime | None = None,
    ) -> None:
        super().__init__(returncode, cmd, stderr=stderr)
        self.synthetic_texts: list[str] = synthetic_texts or []
        self.log_tail: str = log_tail
        self.rate_limit_reset_at: datetime | None = rate_limit_reset_at

    def __str__(self) -> str:
        parts = [_msg("claude_process_cli_exited", returncode=self.returncode)]
        if self.synthetic_texts:
            parts.append("")
            parts.append(_ui()["claude_process_synthetic_messages_header"])
            for text in self.synthetic_texts:
                parts.append(_msg("claude_process_indented_line", text=text.strip()))
        if self.stderr:
            parts.append("")
            parts.append(_ui()["claude_process_stderr_tail_header"])
            parts.append(str(self.stderr)[-2000:])
        if self.log_tail:
            parts.append("")
            parts.append(_ui()["claude_process_stream_log_tail_header"])
            parts.append(self.log_tail)
        if not self.synthetic_texts and not self.stderr and not self.log_tail:
            parts.append(_ui()["claude_process_silent_exit"])
        return "\n".join(parts)


class ClaudeUsageLimitError(ClaudeInvocationError):
    """Claude CLI reported a usage / rate limit hit."""


class ClaudeSubscriptionUnavailableError(ClaudeInvocationError):
    """Claude CLI reported that its OAuth account has no Claude Code subscription."""


class ClaudeAuthenticationError(ClaudeInvocationError):
    """Claude CLI reported missing, expired, or rejected authentication."""


class ClaudeProviderConfigurationError(ClaudeInvocationError):
    """Claude CLI reported provider-specific endpoint/model configuration failure."""


class ClaudeInvocationTerminatedError(RuntimeError):
    """The parent process requested graceful invocation shutdown."""

    def __init__(self, *, signal_num: int, returncode: int | None, stderr: str | None) -> None:
        self.signal_num = signal_num
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            _msg(
                "claude_process_invocation_interrupted",
                signal_num=signal_num,
                child_returncode=(
                    _msg("claude_process_child_returncode", returncode=returncode) if returncode is not None else ""
                ),
            )
        )


class ClaudeProcessError(subprocess.CalledProcessError):
    """Raised when a Claude subprocess exits non-zero."""

    def __init__(
        self,
        *,
        returncode: int,
        cmd: list[str] | tuple[str, ...],
        result_info: dict[str, str | float | int | None],
        result_text: str | None,
    ) -> None:
        super().__init__(returncode=returncode, cmd=list(cmd), output=result_text)
        self.result_info = result_info
        self.result_text = result_text


_USAGE_LIMIT_KEYWORDS = ("hit your limit", "usage limit", "rate limit", "rate_limit_error", "quota", "resets ")
_SUBSCRIPTION_UNAVAILABLE_KEYWORDS = (
    "organization has disabled claude subscription access",
    "subscription access for claude code",
    "claude code requires a paid subscription",
    "claude code requires a subscription",
    "does not have a claude pro or max subscription",
)
_AUTHENTICATION_KEYWORDS = (
    "not logged in",
    "please run /login",
    "authentication failed",
    "invalid api key",
    "invalid x-api-key",
    "oauth token",
)
_PROVIDER_CONFIGURATION_KEYWORDS = (
    "invalid model name",
    "model name passed",
    "/v1/models",
    "claude gateway",
    "gateway diagnostics",
    "empty anthropic stream",
    "malformed anthropic stream",
)
_USAGE_LIMIT_RESET_RE = re.compile(
    r"\bresets?\s+"
    r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\)|\s+(?P<tz_plain>[A-Za-z_/]+))?",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def classify_synthetic_error(
    returncode: int,
    cmd: list[str],
    *,
    stderr: str | None,
    synthetic_texts: list[str],
    log_tail: str,
    rate_limit_reset_at: datetime | None = None,
) -> ClaudeInvocationError:
    """Pick the most specific exception class for a non-zero claude exit."""
    joined = " ".join([*synthetic_texts, stderr or "", log_tail]).lower()
    if any(keyword in joined for keyword in _SUBSCRIPTION_UNAVAILABLE_KEYWORDS):
        return ClaudeSubscriptionUnavailableError(
            returncode,
            cmd,
            stderr=stderr,
            synthetic_texts=synthetic_texts,
            log_tail=log_tail,
            rate_limit_reset_at=rate_limit_reset_at,
        )
    if any(keyword in joined for keyword in _USAGE_LIMIT_KEYWORDS):
        return ClaudeUsageLimitError(
            returncode,
            cmd,
            stderr=stderr,
            synthetic_texts=synthetic_texts,
            log_tail=log_tail,
            rate_limit_reset_at=rate_limit_reset_at,
        )
    if any(keyword in joined for keyword in _AUTHENTICATION_KEYWORDS):
        return ClaudeAuthenticationError(
            returncode,
            cmd,
            stderr=stderr,
            synthetic_texts=synthetic_texts,
            log_tail=log_tail,
            rate_limit_reset_at=rate_limit_reset_at,
        )
    if any(keyword in joined for keyword in _PROVIDER_CONFIGURATION_KEYWORDS):
        return ClaudeProviderConfigurationError(
            returncode,
            cmd,
            stderr=stderr,
            synthetic_texts=synthetic_texts,
            log_tail=log_tail,
            rate_limit_reset_at=rate_limit_reset_at,
        )
    return ClaudeInvocationError(
        returncode,
        cmd,
        stderr=stderr,
        synthetic_texts=synthetic_texts,
        log_tail=log_tail,
        rate_limit_reset_at=rate_limit_reset_at,
    )


def parse_usage_limit_reset_time(message: str, *, now: datetime | None = None) -> datetime | None:
    """Parse Claude Code's usage-limit reset hint into a future UTC timestamp."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    match = _USAGE_LIMIT_RESET_RE.search(message)
    if not match:
        return None

    tz_text = (match.group("tz") or match.group("tz_plain") or "UTC").strip()
    reset_tz: tzinfo
    if tz_text.upper() == "UTC":
        reset_tz = timezone.utc
        now_in_reset_tz = now_utc
    else:
        try:
            reset_tz = ZoneInfo(tz_text)
        except ZoneInfoNotFoundError:  # EXPECTED_EXCEPTION - unknown reset timezone leaves token eligible.
            return None
        now_in_reset_tz = now_utc.astimezone(reset_tz)

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    ampm = match.group("ampm").lower()
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    month_name = match.group("month")
    explicit_year = match.group("year")
    if month_name is None:
        candidate = datetime.combine(now_in_reset_tz.date(), datetime_time(hour, minute), tzinfo=reset_tz)
        if candidate <= now_in_reset_tz:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    day = int(match.group("day"))
    year = int(explicit_year) if explicit_year else now_in_reset_tz.year
    if day < 1 or day > calendar.monthrange(year, month)[1]:
        return None
    candidate = datetime(year, month, day, hour, minute, tzinfo=reset_tz)

    if candidate <= now_in_reset_tz:
        if explicit_year:
            return None
        next_year = candidate.year + 1
        if day > calendar.monthrange(next_year, month)[1]:
            return None
        candidate = datetime(next_year, month, day, hour, minute, tzinfo=reset_tz)
    return candidate.astimezone(timezone.utc)


def usage_limit_reset_time_from_exception(exc: ClaudeUsageLimitError) -> datetime | None:
    if exc.rate_limit_reset_at is not None:
        return exc.rate_limit_reset_at.astimezone(timezone.utc)
    for text in exc.synthetic_texts:
        parsed = parse_usage_limit_reset_time(text)
        if parsed is not None:
            return parsed
    return None
