from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias


@dataclass(frozen=True)
class CLIAuth:
    """Use Claude Code's saved login; remove inherited provider credentials."""


@dataclass(frozen=True)
class OAuthAuth:
    token: str = field(repr=False)


@dataclass(frozen=True)
class AnthropicAuth:
    base_url: str
    auth_token: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    custom_headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OpenAIAuth:
    base_url: str
    model: str
    auth_token: str = field(repr=False)
    forced_model: str | None = None
    custom_headers: dict[str, str] | None = field(default=None, repr=False)


Auth: TypeAlias = CLIAuth | OAuthAuth | AnthropicAuth | OpenAIAuth

AUTH_KEYS = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
    }
)


def build_environment(
    auth: CLIAuth | OAuthAuth | AnthropicAuth,
    base: Mapping[str, str],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if overrides and AUTH_KEYS.intersection(overrides):
        raise ValueError("Set provider credentials through auth, not env_overrides")
    env = {key: value for key, value in base.items() if key not in AUTH_KEYS}
    env.update(overrides or {})
    if isinstance(auth, OAuthAuth):
        if not auth.token:
            raise ValueError("OAuth token must not be empty")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = auth.token
    elif isinstance(auth, AnthropicAuth):
        if auth.auth_token and auth.api_key:
            raise ValueError("Supply auth_token or api_key, not both")
        env["ANTHROPIC_BASE_URL"] = auth.base_url
        if auth.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth.auth_token
        if auth.api_key:
            env["ANTHROPIC_API_KEY"] = auth.api_key
        if auth.custom_headers:
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in auth.custom_headers.items())
    return env
