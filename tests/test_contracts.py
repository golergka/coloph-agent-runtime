from __future__ import annotations

import json
from pathlib import Path

import pytest

from coloph_agent_runtime import AnthropicAuth, CLIAuth, OAuthAuth, OpenAIAuth
from coloph_agent_runtime.argv import build_claude_argv
from coloph_agent_runtime.auth import build_environment
from coloph_agent_runtime.gateway import build_litellm_config, guard_anthropic_messages_response
from coloph_agent_runtime.transcripts import repair_transcript_file
from coloph_agent_runtime.version import ClaudeCodeVersionError, require_supported_claude_code_version


def test_auth_is_explicit_and_credentials_are_not_in_repr():
    assert "secret" not in repr(OAuthAuth("secret"))
    assert "secret" not in repr(AnthropicAuth("https://example.test", api_key="secret"))
    assert "secret" not in repr(OpenAIAuth("https://example.test", "model", "secret"))
    assert build_environment(CLIAuth(), {"ANTHROPIC_API_KEY": "secret", "PATH": "/bin"}) == {"PATH": "/bin"}
    with pytest.raises(ValueError):
        build_environment(CLIAuth(), {}, {"ANTHROPIC_API_KEY": "secret"})


def test_argv_transports_schema_tools_and_budget():
    argv = build_claude_argv(
        "prompt",
        allowed_tools=["Read"],
        permission_mode=None,
        disallowed_tools=["Bash"],
        max_budget_usd=0.25,
        json_schema={"type": "object"},
    )
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert argv[argv.index("--disallowed-tools") + 1] == "Bash"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}


def test_transcript_repair_only_removes_standalone_empty_thinking(tmp_path):
    path = tmp_path / "session.jsonl"
    empty = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}}
    text = {"type": "assistant", "message": {"content": [{"type": "text", "text": "keep"}]}}
    path.write_text(json.dumps(empty) + "\n" + json.dumps(text) + "\n")
    assert repair_transcript_file(path) == 1
    assert json.loads(path.read_text()) == text
    empty["message"]["content"].append({"type": "text", "text": "keep"})
    original = json.dumps(empty)
    path.write_text(original)
    with pytest.raises(ValueError):
        repair_transcript_file(path)
    assert path.read_text() == original


def test_gateway_config_and_empty_response_guard():
    provider = OpenAIAuth("https://example.test/v1", "model", "secret")
    config = build_litellm_config(provider=provider, gateway_key="test")
    assert config["model_list"][0]["litellm_params"]["model"] == "openai/model"
    result = guard_anthropic_messages_response(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps({"type": "message", "content": [], "usage": {"output_tokens": 0}}).encode(),
    )
    assert result.status_code == 502


def test_gateway_converts_tool_use_to_valid_sse():
    result = guard_anthropic_messages_response(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "type": "message",
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a"}}],
                "usage": {"output_tokens": 1},
                "stop_reason": "tool_use",
            }
        ).encode(),
    )
    assert result.status_code == 200
    assert b"input_json_delta" in result.body
    assert b"message_stop" in result.body


def test_version_check(fake_claude):
    assert require_supported_claude_code_version(str(fake_claude)) == (2, 1, 215)
    fake_claude.write_text(fake_claude.read_text().replace("2.1.215", "2.1.1"))
    with pytest.raises(ClaudeCodeVersionError):
        require_supported_claude_code_version(str(fake_claude))


def test_core_has_no_application_imports():
    root = Path(__file__).parents[1] / "src/coloph_agent_runtime"
    for path in root.glob("*.py"):
        source = path.read_text()
        assert "from core." not in source
        assert "import envvar" not in source
        assert "sentry_sdk" not in source
