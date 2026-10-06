from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from coloph_agent_runtime._messages import message as _msg
from coloph_agent_runtime.gateway import (
    anthropic_request_for_litellm,
    gateway_failure_diagnostic,
    guard_anthropic_messages_response,
)


def create_app(
    *,
    inner_base_url: str,
    auth_token: str,
    inner_auth_token: str,
    diagnostics_path: Path,
) -> Starlette:
    async def proxy(request: Request) -> Response:
        if request.headers.get("authorization") != f"Bearer {auth_token}":
            payload = {
                "type": "error",
                "error": {"type": "authentication_error", "message": _msg("claude_gateway_invalid_token")},
            }
            return Response(
                json.dumps(payload).encode("utf-8"),
                status_code=401,
                media_type="application/json",
            )

        body = await request.body()
        if request.url.path == "/v1/messages":
            upstream_request_body = anthropic_request_for_litellm(body)
        else:
            upstream_request_body = body
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "authorization"}
        }
        headers["authorization"] = f"Bearer {inner_auth_token}"
        url = f"{inner_base_url}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            upstream = await client.request(request.method, url, headers=headers, content=upstream_request_body)
        upstream_body = upstream.content
        if request.url.path == "/v1/messages":
            guarded = guard_anthropic_messages_response(
                status_code=upstream.status_code,
                headers=upstream.headers,
                body=upstream_body,
            )
            if guarded.status_code != 200:
                _write_diagnostic(
                    diagnostics_path,
                    gateway_failure_diagnostic(
                        request_path=request.url.path,
                        upstream_status_code=upstream.status_code,
                        upstream_headers=upstream.headers,
                        upstream_body=upstream_body,
                        guarded_status_code=guarded.status_code,
                        guarded_media_type=guarded.media_type,
                        guarded_body=guarded.body,
                    ),
                )
            return Response(
                guarded.body,
                status_code=guarded.status_code,
                headers=guarded.headers,
                media_type=guarded.media_type,
            )

        return Response(
            upstream_body,
            status_code=upstream.status_code,
            headers=_safe_response_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )

    return Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])])


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }


def _write_diagnostic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--inner-base-url", required=True)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--inner-auth-token", required=True)
    parser.add_argument("--diagnostics-path", type=Path, required=True)
    args = parser.parse_args()

    app = create_app(
        inner_base_url=args.inner_base_url,
        auth_token=args.auth_token,
        inner_auth_token=args.inner_auth_token,
        diagnostics_path=args.diagnostics_path,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
