"""Self-hosted LiteLLM gateway for Claude Code OpenAI-compatible providers."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

import httpx
import yaml

from coloph_agent_runtime._diagnostics import report_exception
from coloph_agent_runtime._messages import message as _msg
from coloph_agent_runtime.runner import _signal_process

_LOCALHOST = "127.0.0.1"
_TERMINATE_TIMEOUT_SECONDS = 5
_KILL_WAIT_TIMEOUT_SECONDS = 2
_READINESS_TIMEOUT_SECONDS = 30.0
_READINESS_INTERVAL_SECONDS = 0.25
_ANTHROPIC_JSON_MEDIA_TYPE = "application/json"
_ANTHROPIC_SSE_MEDIA_TYPE = "text/event-stream"


@dataclass(frozen=True)
class ClaudeGateway:
    base_url: str
    auth_token: str
    stdout_path: Path
    stderr_path: Path
    diagnostics_path: Path


@dataclass(frozen=True)
class GuardedGatewayResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    media_type: str


class OpenAICompatibleProvider(Protocol):
    @property
    def base_url(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def forced_model(self) -> str | None: ...

    @property
    def auth_token(self) -> str: ...

    @property
    def custom_headers(self) -> dict[str, str] | None: ...


def _litellm_model_id(model: str) -> str:
    if "/" in model:
        return model
    return f"openai/{model}"


def build_litellm_config(*, provider: OpenAICompatibleProvider, gateway_key: str) -> dict[str, Any]:
    """Build a single-provider LiteLLM config for Claude Code Anthropic requests."""
    forced_model = getattr(provider, "forced_model", None)
    upstream_model = forced_model or provider.model
    params: dict[str, object] = {
        "model": _litellm_model_id(upstream_model),
        "api_base": provider.base_url,
        "api_key": provider.auth_token,
    }
    if provider.custom_headers:
        params["extra_headers"] = dict(provider.custom_headers)
    return {
        "model_list": [
            {
                "model_name": "*" if forced_model else provider.model,
                "litellm_params": params,
            }
        ],
        "general_settings": {
            "master_key": gateway_key,
        },
        "litellm_settings": {
            "drop_params": True,
        },
    }


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOCALHOST, 0))
        return int(sock.getsockname()[1])


def _wait_for_litellm_ready(
    *,
    proc: subprocess.Popen[str],
    base_url: str,
    auth_token: str,
    stderr_path: Path,
    timeout_seconds: float = _READINESS_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    headers = {"Authorization": f"Bearer {auth_token}"}
    last_error = ""
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            from coloph_agent_runtime.runner import ClaudeCancelledError

            raise ClaudeCancelledError("Gateway startup was cancelled")
        returncode = proc.poll()
        if returncode is not None:
            raise RuntimeError(
                _msg(
                    "claude_gateway_exited_before_ready",
                    returncode=returncode,
                    log_tail=_read_log_tail(stderr_path),
                )
            )
        try:
            response = httpx.get(
                f"{base_url}/models", headers=headers, timeout=max(0.001, min(1.0, deadline - time.monotonic()))
            )
            if response.status_code == 200:
                return
            last_error = _msg(
                "claude_gateway_http_readiness_error", status_code=response.status_code, text=response.text[:200]
            )
        except httpx.HTTPError as exc:  # EXPECTED_EXCEPTION: readiness loop retries until the proxy starts.
            last_error = str(exc)
        time.sleep(min(_READINESS_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
    suffix = f": {last_error}" if last_error else ""
    log_suffix = _read_log_tail(stderr_path)
    if log_suffix:
        suffix = _msg("claude_gateway_readiness_stderr_suffix", suffix=suffix, stderr=log_suffix)
    raise TimeoutError(
        _msg("claude_gateway_readiness_timeout", timeout_seconds=f"{timeout_seconds:.1f}", suffix=suffix)
    )


def _read_log_tail(path: Path, *, max_chars: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report_exception(exc, operation="claude_gateway.read_log_tail")
        return ""
    return text[-max_chars:].strip()


def guard_anthropic_messages_response(
    *,
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> GuardedGatewayResponse:
    """Validate a proxied Anthropic /v1/messages response before Claude sees success."""
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    if status_code < 200 or status_code >= 300:
        return _anthropic_error_response(
            status_code=status_code,
            error_type=_error_type_for_status(status_code, body),
            message=_upstream_error_message(body)
            or _msg("claude_gateway_upstream_http_error", status_code=status_code),
            retry_after=normalized_headers.get("retry-after") if status_code == 429 else None,
        )

    content_type = normalized_headers.get("content-type", "")
    if _ANTHROPIC_JSON_MEDIA_TYPE in content_type:
        return _guard_anthropic_json(body)
    if _ANTHROPIC_SSE_MEDIA_TYPE not in content_type:
        return _proxy_error(_msg("claude_gateway_expected_anthropic_stream"))

    return _guard_anthropic_sse(body)


def anthropic_request_for_litellm(body: bytes) -> bytes:
    """Force LiteLLM's Anthropic adapter to use a non-streaming upstream call."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        report_exception(exc, operation="claude_gateway.force_non_streaming_request")
        return body
    if not isinstance(payload, dict):
        return body
    payload["stream"] = False
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _guard_anthropic_json(body: bytes) -> GuardedGatewayResponse:
    # Contract pinned by test_litellm_gateway_forces_non_streaming_upstream_and_preserves_error_contract:
    # LiteLLM's Anthropic adapter calls OpenAI /v1/responses when stream is false.
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        report_exception(exc, operation="claude_gateway.guard_anthropic_json_response")
        return _proxy_error(_msg("claude_gateway_malformed_anthropic_json_upstream"))
    if not isinstance(payload, dict):
        return _proxy_error(_msg("claude_gateway_non_object_anthropic_json_upstream"))
    if payload.get("type") == "error" or isinstance(payload.get("error"), dict):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        assert isinstance(error, dict)
        error_type = str(error.get("type") or "api_error")
        message = str(error.get("message") or _msg("claude_gateway_upstream_json_error"))
        return _anthropic_error_response(
            status_code=_status_for_anthropic_error_type(error_type),
            error_type=error_type,
            message=message,
        )

    usage = payload.get("usage")
    output_tokens = _output_token_count(usage)
    content = payload.get("content")
    if not isinstance(content, list):
        return _proxy_error(_msg("claude_gateway_missing_content_blocks"))
    response_evidence_seen = output_tokens > 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text") or ""):
            response_evidence_seen = True
        elif block.get("type") == "tool_use":
            response_evidence_seen = True
    if not response_evidence_seen:
        return _proxy_error(_msg("claude_gateway_empty_anthropic_message"))
    return GuardedGatewayResponse(
        status_code=200,
        headers={"content-type": _ANTHROPIC_SSE_MEDIA_TYPE},
        body=_anthropic_json_to_sse(payload),
        media_type=_ANTHROPIC_SSE_MEDIA_TYPE,
    )


def _anthropic_json_to_sse(payload: dict[str, Any]) -> bytes:
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    message = dict(payload)
    message["content"] = []
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": message,
            },
        )
    ]
    for index, raw_block in enumerate(blocks):
        if not isinstance(raw_block, dict):
            continue
        block_type = raw_block.get("type")
        if block_type == "text":
            text = str(raw_block.get("text") or "")
            text_block = {"type": "text", "text": ""}
            events.append(
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": text_block},
                )
            )
            if text:
                events.append(
                    (
                        "content_block_delta",
                        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
                    )
                )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        elif block_type == "tool_use":
            tool_block: dict[str, object] = dict(raw_block)
            input_payload = tool_block.pop("input", None)
            tool_block["input"] = {}
            events.append(
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": tool_block},
                )
            )
            if input_payload is not None:
                events.append(
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(input_payload, separators=(",", ":")),
                            },
                        },
                    )
                )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": payload.get("stop_reason"),
                    "stop_sequence": payload.get("stop_sequence"),
                },
                "usage": {"output_tokens": _output_token_count(payload.get("usage"))},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    chunks = []
    for event_name, event_payload in events:
        chunks.append(f"event: {event_name}\n")
        chunks.append(f"data: {json.dumps(event_payload, separators=(',', ':'))}\n\n")
    return "".join(chunks).encode("utf-8")


def _guard_anthropic_sse(body: bytes) -> GuardedGatewayResponse:
    message_start_seen = False
    message_stop_seen = False
    response_evidence_seen = False
    output_tokens = 0
    for event_name, payload in _iter_sse_events(body):
        if event_name == "error" or payload.get("type") == "error":
            raw_error = payload.get("error")
            error = raw_error if isinstance(raw_error, dict) else payload
            error_type = str(error.get("type") or "api_error")
            message = str(error.get("message") or _msg("claude_gateway_upstream_stream_error"))
            return _anthropic_error_response(
                status_code=_status_for_anthropic_error_type(error_type),
                error_type=error_type,
                message=message,
            )
        payload_type = payload.get("type")
        if payload_type == "message_start":
            message_start_seen = True
        elif payload_type == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                response_evidence_seen = True
        elif payload_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and str(delta.get("text") or delta.get("partial_json") or ""):
                response_evidence_seen = True
        elif payload_type == "message_delta":
            output_tokens += _output_token_count(payload.get("usage"))
        elif payload_type == "message_stop":
            message_stop_seen = True

    if not message_start_seen or not message_stop_seen:
        return _proxy_error(_msg("claude_gateway_malformed_anthropic_stream"))
    if not response_evidence_seen and output_tokens <= 0:
        return _proxy_error(_msg("claude_gateway_empty_anthropic_stream"))
    return GuardedGatewayResponse(
        status_code=200,
        headers={"content-type": _ANTHROPIC_SSE_MEDIA_TYPE},
        body=body,
        media_type=_ANTHROPIC_SSE_MEDIA_TYPE,
    )


def gateway_failure_diagnostic(
    *,
    request_path: str,
    upstream_status_code: int,
    upstream_headers: Mapping[str, str],
    upstream_body: bytes,
    guarded_status_code: int,
    guarded_media_type: str,
    guarded_body: bytes,
) -> dict[str, object]:
    normalized_headers = {str(key).lower(): str(value) for key, value in upstream_headers.items()}
    content_type = normalized_headers.get("content-type", "")
    return {
        "request_path": request_path,
        "status_code": guarded_status_code,
        "media_type": guarded_media_type,
        "body": guarded_body.decode("utf-8", errors="replace"),
        "upstream_status_code": upstream_status_code,
        "upstream_content_type": content_type,
        "upstream_summary": _upstream_body_summary(content_type=content_type, body=upstream_body),
    }


def _upstream_body_summary(*, content_type: str, body: bytes) -> dict[str, object]:
    if _ANTHROPIC_SSE_MEDIA_TYPE in content_type:
        event_counts: Counter[str] = Counter()
        payload_type_counts: Counter[str] = Counter()
        output_tokens = 0
        text_delta_chars = 0
        tool_use_blocks = 0
        for event_name, payload in _iter_sse_events(body):
            if event_name:
                event_counts[event_name] += 1
            payload_type = payload.get("type")
            if isinstance(payload_type, str):
                payload_type_counts[payload_type] += 1
            if payload_type == "message_delta":
                output_tokens += _output_token_count(payload.get("usage"))
            elif payload_type == "content_block_start":
                block = payload.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_blocks += 1
            elif payload_type == "content_block_delta":
                delta = payload.get("delta")
                if isinstance(delta, dict):
                    text_delta_chars += len(str(delta.get("text") or delta.get("partial_json") or ""))
        return {
            "kind": "anthropic_sse",
            "event_counts": dict(sorted(event_counts.items())),
            "payload_type_counts": dict(sorted(payload_type_counts.items())),
            "output_tokens": output_tokens,
            "text_delta_chars": text_delta_chars,
            "tool_use_blocks": tool_use_blocks,
        }
    return {
        "kind": "body_preview",
        "body_preview": _redacted_text_preview(body),
    }


def _redacted_text_preview(body: bytes, *, max_chars: int = 2000) -> str:
    text = body.decode("utf-8", errors="replace")[:max_chars]
    text = re.sub(r"\bsk-[A-Za-z0-9._=-]+", "[REDACTED]", text)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._=-]+", "[REDACTED]", text)
    return text


def _iter_sse_events(body: bytes) -> Iterator[tuple[str | None, dict[str, Any]]]:
    text = body.decode("utf-8", errors="replace")
    for raw_event in text.split("\n\n"):
        event_name: str | None = None
        data_lines: list[str] = []
        for raw_line in raw_event.splitlines():
            line = raw_line.rstrip("\r")
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:  # EXPECTED_EXCEPTION: malformed SSE data becomes an Anthropic proxy error.
            yield (
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": _msg("claude_gateway_malformed_anthropic_json"),
                    },
                },
            )
            continue
        if not isinstance(payload, dict):
            yield (
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": _msg("claude_gateway_non_object_anthropic_json"),
                    },
                },
            )
            continue
        yield event_name, payload


def _output_token_count(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get("output_tokens")
    return max(value, 0) if isinstance(value, int) else 0


def _error_type_for_status(status_code: int, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").lower()
    if status_code == 429 or "insufficient_quota" in text or "rate limit" in text or "rate_limit" in text:
        return "rate_limit_error"
    if status_code in (401, 403):
        return "authentication_error"
    if status_code == 400:
        return "invalid_request_error"
    return "api_error"


def _status_for_anthropic_error_type(error_type: str) -> int:
    if error_type == "rate_limit_error":
        return 429
    if error_type == "authentication_error":
        return 401
    if error_type == "invalid_request_error":
        return 400
    return 502


def _upstream_error_message(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:  # EXPECTED_EXCEPTION: non-JSON upstream errors are surfaced as text.
        text = body.decode("utf-8", errors="replace").strip()
        return text[:2000] or None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("type")
        return str(message) if message else None
    if isinstance(error, str):
        return error
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    message = payload.get("message")
    return str(message) if message else None


def _anthropic_error_response(
    *,
    status_code: int,
    error_type: str,
    message: str,
    retry_after: str | None = None,
) -> GuardedGatewayResponse:
    headers = {"content-type": _ANTHROPIC_JSON_MEDIA_TYPE}
    if retry_after:
        headers["retry-after"] = retry_after
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return GuardedGatewayResponse(
        status_code=status_code,
        headers=headers,
        body=body,
        media_type=_ANTHROPIC_JSON_MEDIA_TYPE,
    )


def _proxy_error(message: str) -> GuardedGatewayResponse:
    return _anthropic_error_response(status_code=502, error_type="api_error", message=message)


def _litellm_executable() -> str:
    script_name = "litellm.exe" if os.name == "nt" else "litellm"
    executable = Path(sys.executable).with_name(script_name)
    if not executable.is_file():
        raise RuntimeError(_msg("claude_gateway_executable_missing", executable=executable))
    return str(executable)


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        _signal_process(proc, signal.SIGKILL)
        return
    _signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:  # EXPECTED_EXCEPTION: unresponsive proxy process is force-killed during cleanup.
        _kill_process(proc)
    finally:
        _signal_process(proc, signal.SIGKILL)


def _kill_process(proc: subprocess.Popen[str]) -> None:
    _signal_process(proc, signal.SIGKILL)
    proc.wait(timeout=_KILL_WAIT_TIMEOUT_SECONDS)


@contextmanager
def start_litellm_gateway(
    *,
    provider: OpenAICompatibleProvider,
    gateway_key: str | None = None,
    temp_dir: Path | None = None,
    readiness_timeout_seconds: float = _READINESS_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
) -> Iterator[ClaudeGateway]:
    """Start a local LiteLLM Anthropic-compatible proxy for one provider."""
    key = gateway_key or f"sk-coloph-agent-runtime-{secrets.token_urlsafe(24)}"
    inner_port = _reserve_local_port()
    inner_base_url = f"http://{_LOCALHOST}:{inner_port}"

    if temp_dir is None:
        with tempfile.TemporaryDirectory(prefix="coloph-agent-runtime-") as raw_dir:
            yield from _run_gateway(
                provider=provider,
                gateway_key=key,
                inner_base_url=inner_base_url,
                inner_port=inner_port,
                temp_dir=Path(raw_dir),
                readiness_timeout_seconds=readiness_timeout_seconds,
                cancel=cancel,
            )
        return

    yield from _run_gateway(
        provider=provider,
        gateway_key=key,
        inner_base_url=inner_base_url,
        inner_port=inner_port,
        temp_dir=temp_dir,
        readiness_timeout_seconds=readiness_timeout_seconds,
        cancel=cancel,
    )


def _run_gateway(
    *,
    provider: OpenAICompatibleProvider,
    gateway_key: str,
    inner_base_url: str,
    inner_port: int,
    temp_dir: Path,
    readiness_timeout_seconds: float,
    cancel: threading.Event | None,
) -> Iterator[ClaudeGateway]:
    deadline = time.monotonic() + readiness_timeout_seconds
    temp_dir.mkdir(parents=True, exist_ok=True)
    config_path = temp_dir / "litellm.yaml"
    stdout_path = temp_dir / "litellm.stdout.log"
    stderr_path = temp_dir / "litellm.stderr.log"
    diagnostics_path = temp_dir / "claude-gateway.diagnostics.jsonl"
    outer_port = _reserve_local_port()
    outer_base_url = f"http://{_LOCALHOST}:{outer_port}"
    config_path.write_text(
        yaml.safe_dump(build_litellm_config(provider=provider, gateway_key=gateway_key), sort_keys=False),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            [
                _litellm_executable(),
                "--config",
                str(config_path),
                "--host",
                _LOCALHOST,
                "--port",
                str(inner_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            try:
                _wait_for_litellm_ready(
                    proc=proc,
                    base_url=inner_base_url,
                    auth_token=gateway_key,
                    stderr_path=stderr_path,
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                    cancel=cancel,
                )
            except (RuntimeError, TimeoutError):
                _kill_process(proc)
                raise
            gateway_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coloph_agent_runtime.gateway_app",
                    "--host",
                    _LOCALHOST,
                    "--port",
                    str(outer_port),
                    "--inner-base-url",
                    inner_base_url,
                    "--auth-token",
                    gateway_key,
                    "--inner-auth-token",
                    gateway_key,
                    "--diagnostics-path",
                    str(diagnostics_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
            try:
                try:
                    _wait_for_litellm_ready(
                        proc=gateway_proc,
                        base_url=outer_base_url,
                        auth_token=gateway_key,
                        stderr_path=stderr_path,
                        timeout_seconds=max(0.0, deadline - time.monotonic()),
                        cancel=cancel,
                    )
                except (RuntimeError, TimeoutError):
                    _kill_process(gateway_proc)
                    raise
                yield ClaudeGateway(
                    base_url=outer_base_url,
                    auth_token=gateway_key,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    diagnostics_path=diagnostics_path,
                )
            finally:
                _stop_process(gateway_proc)
        finally:
            _stop_process(proc)
