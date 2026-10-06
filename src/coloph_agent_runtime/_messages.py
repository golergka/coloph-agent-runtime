"""Stable English diagnostics; applications own presentation."""

_MESSAGES: dict[str, str] = {
    "claude_gateway_empty_anthropic_message": "Claude gateway rejected an empty Anthropic message.",
    "claude_gateway_empty_anthropic_stream": "Claude gateway rejected an empty Anthropic stream.",
    "claude_gateway_executable_missing": "LiteLLM executable not found at {executable}; install project dependencies before starting gateway.",
    "claude_gateway_exited_before_ready": "LiteLLM gateway exited before readiness check completed (exit {returncode}): {log_tail}",
    "claude_gateway_expected_anthropic_stream": "Claude gateway expected an Anthropic event stream from upstream.",
    "claude_gateway_http_readiness_error": "HTTP {status_code}: {text}",
    "claude_gateway_invalid_token": "Invalid gateway token.",
    "claude_gateway_malformed_anthropic_json": "Claude gateway received malformed JSON in Anthropic stream.",
    "claude_gateway_malformed_anthropic_json_upstream": "Claude gateway received malformed Anthropic JSON from upstream.",
    "claude_gateway_malformed_anthropic_stream": "Claude gateway received a malformed Anthropic stream.",
    "claude_gateway_missing_content_blocks": "Claude gateway received Anthropic JSON without content blocks.",
    "claude_gateway_non_object_anthropic_json": "Claude gateway received non-object JSON in Anthropic stream.",
    "claude_gateway_non_object_anthropic_json_upstream": "Claude gateway received non-object Anthropic JSON from upstream.",
    "claude_gateway_readiness_stderr_suffix": "{suffix}; stderr: {stderr}",
    "claude_gateway_readiness_timeout": "LiteLLM gateway did not become ready within {timeout_seconds}s{suffix}",
    "claude_gateway_upstream_http_error": "Upstream provider returned HTTP {status_code}",
    "claude_gateway_upstream_json_error": "Upstream provider returned an Anthropic JSON error.",
    "claude_gateway_upstream_stream_error": "Upstream provider returned an Anthropic stream error.",
    "claude_process_child_returncode": " (child returncode={returncode})",
    "claude_process_cli_exited": "Claude CLI exited with code {returncode}.",
    "claude_process_indented_line": "  {text}",
    "claude_process_invocation_interrupted": "Invocation interrupted by signal {signal_num}{child_returncode}",
    "claude_process_silent_exit": "(no stderr, no synthetic messages, no log - CLI exited silently)",
    "claude_process_stderr_tail_header": "stderr (last 2KB):",
    "claude_process_stream_log_tail_header": "stream-json log tail (last 4KB):",
    "claude_process_synthetic_messages_header": "Claude Code synthetic messages:",
    "claude_transcript_empty_thinking_mixed_content": "claude transcript {transcript} line {line}: empty thinking block mixed with other content blocks {block_types}; refusing to guess a repair (extend repair_transcript_file once the safe handling is known)",
}


def messages() -> dict[str, str]:
    return _MESSAGES


def message(key: str, **params: object) -> str:
    return _MESSAGES[key].format(**params)
