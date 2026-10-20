"""Explicit portable ChatGPT login cache. Secret material is never represented."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CodexSubscriptionAuth:
    _auth_json: str = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False, compare=False)
    _source: Path | None = field(default=None, repr=False, init=False, compare=False)
    _source_snapshot: str | None = field(default=None, repr=False, init=False, compare=False)

    @classmethod
    def from_local(cls, *, codex_home: Path | None = None) -> CodexSubscriptionAuth:
        home = codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        result = cls.from_file(home / "auth.json")
        result._source = home / "auth.json"
        result._source_snapshot = result._auth_json
        return result

    @classmethod
    def from_file(cls, path: Path) -> CodexSubscriptionAuth:
        try:
            value = path.read_text()
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "No file-based Codex login found; run codex login with file credential storage"
            ) from exc
        result = cls(value)
        result._validate()
        return result

    def _validate(self) -> None:
        data = json.loads(self._auth_json)
        tokens = data.get("tokens")
        if data.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise ValueError("Expected a Codex ChatGPT subscription login cache")

    def _accept_refresh(self, value: str) -> None:
        candidate = CodexSubscriptionAuth(value)
        candidate._validate()
        self._auth_json = value
        if self._source is not None and value != self._source_snapshot:
            if self._source.read_text() != self._source_snapshot:
                raise RuntimeError(
                    "Local Codex login changed during execution; refreshed credentials retained in auth object"
                )
            self.save(self._source)
            self._source_snapshot = value

    def save(self, path: Path) -> None:
        """Persist refreshed credentials explicitly, with atomic replacement and mode 0600."""
        self._validate()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".codex-auth-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(self._auth_json)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
