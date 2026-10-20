"""Runnable application boundary without an external service or account."""

from __future__ import annotations

import json
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from coloph_agent_runtime import ClaudeClient, CodexClient, CodexRunOptions, JsonlRecorder, RunOptions
from coloph_agent_runtime.docker import DockerBackend


def main() -> None:
    assets = files("coloph_agent_runtime")
    assert assets.joinpath("py.typed").is_file()
    assert assets.joinpath("bundled_agent_skills/run-claude-code/SKILL.md").is_file()
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        executable = cwd / "fake-claude"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('2.1.215 (Claude Code)'); sys.exit(0)\n"
            "assert os.environ['APP_PRINCIPAL'] == 'worker'\n"
            "flag = '--resume' if '--resume' in sys.argv else '--session-id'\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
            "    'session_id': sys.argv[sys.argv.index(flag) + 1], 'result': 'complete'}))\n"
        )
        executable.chmod(0o755)
        client = ClaudeClient(executable=str(executable))
        options = RunOptions(allowed_tools=("Read",), timeout_seconds=10)
        with (cwd / "audit.jsonl").open("w", encoding="utf-8") as audit:
            result = client.run(
                "Do the work",
                cwd=cwd,
                options=options,
                env_overrides={"APP_PRINCIPAL": "worker", "APP_INVOCATION_ID": "example-1"},
                on_event=JsonlRecorder(audit),
            )
        # Persist result usage and the application invocation ID in application storage here.
        saved_event = json.loads((cwd / "audit.jsonl").read_text())
        assert saved_event["session_id"] == result.session_id
        continued = client.run(
            "Continue",
            cwd=cwd,
            options=options,
            session_id=result.session_id,
            resume=True,
            env_overrides={"APP_PRINCIPAL": "worker"},
        )
        assert continued.session_id == result.session_id
        assert continued.response_text == "complete"
        codex = cwd / "fake-codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'example-thread'}))\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'complete'}}))\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n"
        )
        codex.chmod(0o755)
        codex_result = CodexClient(executable=str(codex)).run("Do the work", cwd=cwd, options=CodexRunOptions())
        assert codex_result.response_text == "complete" and codex_result.input_tokens == 1
        assert DockerBackend().command == ["docker"]
        print("Application boundary smoke passed")


if __name__ == "__main__":
    main()
