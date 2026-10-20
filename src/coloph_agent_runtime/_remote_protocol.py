"""Versioned JSON protocol shared by the worker and remote clients."""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from datetime import datetime
from typing import Any

from . import errors
from .auth import AnthropicAuth, Auth, CLIAuth, OAuthAuth, OpenAIAuth
from .codex import CodexCancelledError, CodexInvocationError, CodexProtocolError
from .runner import ClaudeCancelledError, ClaudeProtocolError, ClaudeResultError
from .version import ClaudeCodeVersionError

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 32 * 1024 * 1024


class RemoteExecutionError(RuntimeError):
    """The remote worker reported an unexpected failure."""


class RemoteTransportError(RemoteExecutionError):
    """Transport failed. The invocation outcome is uncertain; no retry occurred."""


class WorkerBusyError(RemoteExecutionError):
    """The worker already has an active invocation."""


def encode_auth(auth: Auth) -> dict[str, Any]:
    return {"kind": type(auth).__name__, "values": asdict(auth)}


def decode_auth(value: dict[str, Any]) -> Auth:
    constructors = {c.__name__: c for c in (CLIAuth, OAuthAuth, AnthropicAuth, OpenAIAuth)}
    if value["kind"] not in constructors:
        raise ValueError("Unsupported authentication type")
    return constructors[value["kind"]](**value["values"])


def encode_error(exc: Exception) -> dict[str, Any]:
    data: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, CodexInvocationError):
        data.update(stderr=exc.stderr, events=exc.events, returncode=exc.returncode)
    if isinstance(exc, errors.ClaudeInvocationError):
        data.update(
            returncode=exc.returncode,
            cmd=exc.cmd,
            stderr=exc.stderr,
            synthetic_texts=exc.synthetic_texts,
            log_tail=exc.log_tail,
            rate_limit_reset_at=exc.rate_limit_reset_at.isoformat() if exc.rate_limit_reset_at else None,
        )
    if isinstance(exc, ClaudeResultError):
        data["event"] = exc.terminal_event
    if isinstance(exc, ClaudeProtocolError):
        data.update(events=exc.events, stderr=exc.stderr)
    if isinstance(exc, subprocess.TimeoutExpired):
        data.update(cmd=exc.cmd, timeout=exc.timeout, stderr=exc.stderr, output=exc.output)
    if isinstance(exc, OSError):
        data.update(errno=exc.errno, filename=exc.filename, strerror=exc.strerror)
    return data


def decode_error(data: dict[str, Any]) -> Exception:
    kind = data["type"]
    if kind in {"CodexInvocationError", "CodexProtocolError"}:
        constructor = CodexProtocolError if kind == "CodexProtocolError" else CodexInvocationError
        return constructor(data["message"], stderr=data["stderr"], events=data["events"], returncode=data["returncode"])
    if kind == "CodexCancelledError":
        return CodexCancelledError(data["message"])
    if kind == "ClaudeResultError":
        return ClaudeResultError(data["cmd"], event=data["event"], stderr=data["stderr"])
    invocation_types = {
        c.__name__: c
        for c in (
            errors.ClaudeInvocationError,
            errors.ClaudeUsageLimitError,
            errors.ClaudeAuthenticationError,
            errors.ClaudeProviderConfigurationError,
            errors.ClaudeSubscriptionUnavailableError,
        )
    }
    if kind in invocation_types:
        return invocation_types[kind](
            data["returncode"],
            data["cmd"],
            stderr=data["stderr"],
            synthetic_texts=data["synthetic_texts"],
            log_tail=data["log_tail"],
            rate_limit_reset_at=(
                datetime.fromisoformat(data["rate_limit_reset_at"]) if data["rate_limit_reset_at"] else None
            ),
        )
    if kind == "ClaudeProtocolError":
        return ClaudeProtocolError(data["message"], events=data["events"], stderr=data["stderr"])
    if kind == "TimeoutExpired":
        return subprocess.TimeoutExpired(data["cmd"], data["timeout"], output=data["output"], stderr=data["stderr"])
    if "errno" in data and data["errno"] is not None:
        return OSError(data["errno"], data["strerror"], data["filename"])
    simple: dict[str, type[Exception]] = {
        c.__name__: c
        for c in (
            ValueError,
            TypeError,
            FileNotFoundError,
            NotADirectoryError,
            ClaudeCancelledError,
            ClaudeCodeVersionError,
            WorkerBusyError,
        )
    }
    return simple.get(kind, RemoteExecutionError)(data["message"])
