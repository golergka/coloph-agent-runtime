"""Exercise both real gateway processes against an offline OpenAI-shaped server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from coloph_agent_runtime import OpenAIAuth
from coloph_agent_runtime.gateway import start_litellm_gateway


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["model"] == "offline-model"
        assert self.path == "/v1/responses"
        assert request.get("stream") is not True
        payload = {
            "id": "resp_offline",
            "object": "response",
            "created_at": 1700000000,
            "status": "completed",
            "model": "offline-model",
            "output": [
                {
                    "id": "msg_offline",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "offline answer", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = OpenAIAuth(f"http://127.0.0.1:{server.server_port}/v1", "offline-model", "offline-key")
    try:
        with start_litellm_gateway(provider=provider, readiness_timeout_seconds=30) as gateway:
            response = httpx.post(
                gateway.base_url + "/v1/messages",
                headers={"Authorization": f"Bearer {gateway.auth_token}"},
                json={
                    "model": "offline-model",
                    "max_tokens": 20,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                },
                timeout=30,
            )
            assert response.status_code == 200, response.text
            assert "offline answer" in response.text
            assert "message_stop" in response.text
            temporary = gateway.stdout_path.parent
        assert not temporary.exists()
        try:
            httpx.get(gateway.base_url + "/models", timeout=1)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("Gateway still accepts connections after cleanup")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("Gateway startup, provider translation, and cleanup smoke passed")


if __name__ == "__main__":
    main()
