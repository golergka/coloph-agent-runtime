"""Sequential Docker verification with one CPU, 1 GiB RAM and 256 PIDs per worker."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import threading
from pathlib import Path

from coloph_agent_runtime import (
    CodexCancelledError,
    CodexInvocationError,
    CodexProtocolError,
    CodexRunOptions,
    CodexSubscriptionAuth,
    RunOptions,
)
from coloph_agent_runtime.docker import DockerBackend

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="coloph-agent-runtime:0.3.0-test")
    parser.add_argument("--live", action="store_true", help="Use local ChatGPT subscription for three small real calls")
    args = parser.parse_args()
    backend = DockerBackend()
    with backend.container(image=args.image) as container:
        print("Worker running with 1 CPU / 1 GiB / 256 PIDs", flush=True)
        backend._run("cp", str(ROOT / "tests/fixtures/fake_codex.py"), container.resource_id + ":/tmp/fake_codex")
        # Copy a fake Claude fixture without bringing the repository or credentials into the container.
        import importlib.util

        specification = importlib.util.spec_from_file_location("fixtures", ROOT / "tests/conftest.py")
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            fake = module.fake_claude.__wrapped__(Path(temporary))
            fake.write_text("#!/usr/local/bin/python\n" + fake.read_text().split("\n", 1)[1])
            backend._run("cp", str(fake), container.resource_id + ":/tmp/fake_claude")
        backend._run(
            "exec", "--user", "root", container.resource_id, "chmod", "555", "/tmp/fake_codex", "/tmp/fake_claude"
        )
        runner = container.codex(executable="/tmp/fake_codex")
        options = CodexRunOptions(timeout_seconds=20)
        cwd = Path("/workspace")
        events = []
        result = runner.run("ok", cwd=cwd, options=options, on_event=events.append)
        assert result.response_text == "done" and result.events == events
        assert runner.run("ok", cwd=cwd, options=options, session_id=result.session_id).session_id == result.session_id
        for prompt, error in (
            ("missing", CodexProtocolError),
            ("duplicate", CodexProtocolError),
            ("failure", CodexInvocationError),
        ):
            try:
                runner.run(prompt, cwd=cwd, options=options)
                raise AssertionError("Expected invocation failure")
            except error:
                pass
        try:
            runner.run("sleep", cwd=cwd, options=CodexRunOptions(timeout_seconds=0.5))
            raise AssertionError("Expected deadline")
        except subprocess.TimeoutExpired:
            pass
        cancel = threading.Event()
        try:
            runner.run("sleep", cwd=cwd, options=options, cancel=cancel, on_event=lambda event: cancel.set())
            raise AssertionError("Expected cancellation")
        except CodexCancelledError:
            pass
        large = runner.run("pipes", cwd=cwd, options=options, stdin_text="x" * 200000)
        assert len(large.stderr) == 200000

        def fail_callback(event):
            if event["type"] == "item.completed":
                raise LookupError("callback failed")

        try:
            runner.run("callback", cwd=cwd, options=options, on_event=fail_callback)
            raise AssertionError("Expected callback failure")
        except LookupError:
            pass
        assert runner.run("ok", cwd=cwd, options=options).response_text == "done"
        assert backend.attach(name=container.name).resource_id == container.resource_id
        claude = container.claude(executable="/tmp/fake_claude")
        original = claude.run("ok", cwd=cwd, options=RunOptions(allowed_tools=()))
        assert (
            claude.run("ok", cwd=cwd, options=RunOptions(allowed_tools=()), session_id=original.session_id).session_id
            == original.session_id
        )
        auth = CodexSubscriptionAuth(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token", "account_id": "smoke"}})
        )
        container.codex(executable="/tmp/fake_codex", auth=auth).run("refresh", cwd=cwd, options=options)
        assert "refreshed-test-token" in auth._auth_json
        print("Deterministic Claude/Codex Docker contracts passed", flush=True)
        if args.live:
            auth = CodexSubscriptionAuth.from_local()
            live = container.codex(auth=auth)
            live_options = CodexRunOptions(
                timeout_seconds=120, skip_git_repo_check=True, ignore_user_config=True, effort="low"
            )
            result = live.run("Reply exactly RUNTIME_OK. Do not use any tools.", cwd=cwd, options=live_options)
            assert "RUNTIME_OK" in (result.response_text or "")
            resumed = live.run(
                "Reply exactly RESUME_OK. Do not use any tools.",
                cwd=cwd,
                options=live_options,
                session_id=result.session_id,
            )
            assert "RESUME_OK" in (resumed.response_text or "")
            tool_result = live.run(
                "Use a shell tool to write exactly TOOL_OK to /workspace/tool-result.txt, then reply DONE.",
                cwd=cwd,
                options=CodexRunOptions(
                    timeout_seconds=120,
                    skip_git_repo_check=True,
                    ignore_user_config=True,
                    effort="low",
                    sandbox="danger-full-access",
                ),
            )
            assert tool_result.session_id
            assert (
                backend._run("exec", container.resource_id, "cat", "/workspace/tool-result.txt").stdout.strip()
                == "TOOL_OK"
            )
            print("Live ChatGPT subscription execution and resume passed (credentials omitted)", flush=True)
    assert container.status().phase == "removed"
    container.close()
    print("Docker cleanup passed", flush=True)


if __name__ == "__main__":
    main()
