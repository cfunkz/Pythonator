"""The security timeline: every block, refusal, permission change and self-heal, kept for 30 days.
One JSON object per line in `security.jsonl` (private, append only). Repeats of the same thing within a minute
(a service retrying a blocked host) are folded into one row with a count, so a loop can't flood the file. Rows
are written by the background service only; services and plugins can't add to it."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from pythonator.store.files import append_line, ensure_private_dir

KINDS = ("blocked", "refused", "permission", "healed")
KEEP_S = 30 * 86400
MAX_ROWS = 20000
MAX_BYTES = 4 * 1024 * 1024
FOLD_S = 60.0

class SecurityLog:
    def __init__(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        self.path = path
        self.rows: deque[dict[str, Any]] = deque(maxlen=MAX_ROWS)
        self._next = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        cut = time.time() - KEEP_S
        byid: dict[int, dict[str, Any]] = {}
        with open(self.path, "rb") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(r, dict) or "id" not in r:
                    continue
                if r.get("op") == "count":
                    if r["id"] in byid:
                        byid[r["id"]]["count"] = int(r.get("count", 1))
                        byid[r["id"]]["last"] = float(r.get("last", 0))
                    continue
                if float(r.get("ts", 0)) >= cut and r.get("kind") in KINDS:
                    byid[int(r["id"])] = r
                self._next = max(self._next, int(r["id"]) + 1)
        self.rows.extend(sorted(byid.values(), key=lambda r: r["ts"]))
        if self.path.stat().st_size > MAX_BYTES:
            self._compact()

    def _append(self, rec: dict[str, Any]) -> None:
        append_line(self.path, rec, sync=False)

    def _compact(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for r in self.rows:
                f.write((json.dumps(r, separators=(",", ":")) + "\n").encode())
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, kind: str, who: str, title: str, detail: str = "", action: dict[str, Any] | None = None,
            ts: float | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(kind)
        now = ts or time.time()
        for r in reversed(self.rows):
            if now - r.get("last", r["ts"]) > FOLD_S:
                break
            if r["kind"] == kind and r["who"] == who and r["title"] == title:
                r["count"] = r.get("count", 1) + 1
                r["last"] = now
                self._append({"id": r["id"], "op": "count", "count": r["count"], "last": now})
                return r
        r = {"id": self._next, "kind": kind, "who": who[:80], "title": title[:200], "detail": detail[:300],
             "action": action or {}, "ts": now, "last": now, "count": 1}
        self._next += 1
        self.rows.append(r)
        self._append(r)
        return r

    def since(self, ts: float) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("last", r["ts"]) >= ts]

    def resolve(self, rid: int, how: str) -> dict[str, Any] | None:
        """Mark a row's action as done (Allow pressed, rule opened), so the button doesn't stay."""
        for r in self.rows:
            if r["id"] == rid:
                r["action"] = {**r.get("action", {}), "done": how}
                self._append({**r})
                return r
        return None
