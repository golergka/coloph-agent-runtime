#!/usr/bin/env python3
"""Deterministic Codex CLI stand-in, also used inside Docker."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli 0.144.1")
    raise SystemExit
prompt = sys.stdin.read()
Path("codex.pid").write_text(str(os.getpid()))
Path("codex-argv.json").write_text(json.dumps(sys.argv[1:]))
if "resume" in sys.argv:
    sid = sys.argv[sys.argv.index("resume") + 1]
    assert Path(sid).exists()
else:
    sid = str(uuid.uuid4())
Path(sid).write_text("session")
print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if prompt == "sleep":
    time.sleep(60)
if prompt == "failure":
    print(json.dumps({"type": "turn.failed", "error": {"message": "test failure"}}))
    raise SystemExit(1)
if prompt == "missing":
    raise SystemExit
if prompt == "refresh":
    auth_path = Path(os.environ["CODEX_HOME"]) / "auth.json"
    auth = json.loads(auth_path.read_text())
    auth["tokens"]["access_token"] = "refreshed-test-token"
    auth_path.write_text(json.dumps(auth))
if prompt.startswith("pipes"):
    sys.stderr.write("e" * 200000)
if prompt == "structured":
    text = '{"answer":42}'
else:
    text = "done"
print(json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": text}}), flush=True)
if prompt == "callback":
    time.sleep(60)
terminal = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 4}}
print(json.dumps(terminal), flush=True)
if prompt == "duplicate":
    print(json.dumps(terminal))
