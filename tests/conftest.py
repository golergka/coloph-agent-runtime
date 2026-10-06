from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """A real child process that emits CLI-shaped events without an account."""
    path = tmp_path / "claude"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("2.1.215 (Claude Code)")
    sys.exit(0)
Path("child.pid").write_text(str(os.getpid()))
Path("argv.json").write_text(json.dumps(sys.argv[1:]))
Path("context.json").write_text(json.dumps({
    k: os.environ.get(k) for k in ("APP_ROLE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
}))
mode = sys.argv[sys.argv.index("-p") + 1]
sid_flag = "--resume" if "--resume" in sys.argv else "--session-id"
sid = sys.argv[sys.argv.index(sid_flag) + 1]
if mode == "sleep":
    time.sleep(30)
if mode == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(1.5); Path('survivor').touch()"])
    Path("descendant.pid").write_text(str(child.pid))
    time.sleep(30)
if mode == "missing":
    sys.exit(0)
if mode == "failure":
    print(json.dumps({"type":"assistant", "message":{"model":"<synthetic>", "content":[
        {"type":"text", "text":"You've hit your usage limit"}
    ]}}))
    print(json.dumps({"type":"rate_limit_event", "rate_limit_info":{"resetsAt": 1900000000}}))
    print("diagnostic", file=sys.stderr)
    sys.exit(1)
if mode == "pipes":
    sys.stderr.write("e" * 200000)
    sys.stderr.flush()
    payload = sys.stdin.read()
    Path("input_size").write_text(str(len(payload)))
print("startup noise")
print(json.dumps({"type":"assistant", "message":{"content":[{"type":"text", "text":"working"}]}}), flush=True)
if mode == "callback":
    time.sleep(30)
result = {"type":"result", "subtype":"success", "is_error":False, "session_id":sid,
    "result":"done", "usage":{"input_tokens":4, "output_tokens":2}, "total_cost_usd":0.01}
if mode == "structured":
    result["structured_output"] = {"answer":42}
if mode == "terminal_error":
    result.update(is_error=True, subtype="error_max_turns")
if mode == "empty":
    result.update(result="", usage={"input_tokens":0,"output_tokens":0})
if mode == "bad_usage":
    result["usage"] = "invalid"
print(json.dumps(result), flush=True)
if mode == "duplicate":
    print(json.dumps(result), flush=True)
"""
    )
    path.chmod(0o755)
    return path
