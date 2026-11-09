"""Ownership ledger: every side effect is written down *before* it happens, and marked done after.
If Pythonator crashes between the two, the next start finds an `intended` record with no `done` and finishes or
undoes that step. Containers, scopes and processes Pythonator didn't record are never touched (they belong to
another app). Uninstall removes exactly what the ledger lists.
Format: one JSON object per line, fsync'ed. {"id": 12, "op": "intend"|"done"|"undone", "kind": "process"|...,
"service": "...", "ref": "...", "ts": 1730000000.0, "extra": {...}}"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pythonator.store.files import append_line, ensure_private_dir

MAX_BYTES = 4 * 1024 * 1024

@dataclass
class Entry:
    id: int
    kind: str  # process, container, scope
    service: str
    ref: str  # pid:starttime, container name, unit name
    ts: float
    extra: dict[str, Any] = field(default_factory=dict)
    state: str = "intended"  # intended -> done -> undone

class Ledger:
    def __init__(self, path: Path) -> None:
        ensure_private_dir(path.parent)
        self.path = path
        self.entries: dict[int, Entry] = {}
        self._next = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "rb") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue  # a torn last line after a crash
                rid = int(r.get("id", 0))
                if r.get("op") == "intend":
                    self.entries[rid] = Entry(rid, r["kind"], r["service"], r["ref"], r["ts"], r.get("extra", {}))
                elif rid in self.entries:
                    self.entries[rid].state = "done" if r["op"] == "done" else "undone"
                    if r.get("ref"):
                        self.entries[rid].ref = r["ref"]
                self._next = max(self._next, rid + 1)
        self.entries = {k: v for k, v in self.entries.items() if v.state != "undone"}
        if self.path.stat().st_size > MAX_BYTES:
            self._compact()

    def _append(self, rec: dict[str, Any]) -> None:
        append_line(self.path, rec, sync=True)

    def _compact(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for e in self.entries.values():
                f.write(
                    (
                        json.dumps(
                            {
                                "id": e.id, "op": "intend", "kind": e.kind, "service": e.service, "ref": e.ref,
                                "ts": e.ts, "extra": e.extra,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                if e.state == "done":
                    f.write((json.dumps({"id": e.id, "op": "done"}) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def intend(self, kind: str, service: str, ref: str, **extra: Any) -> Entry:
        e = Entry(self._next, kind, service, ref, time.time(), extra)
        self._next += 1
        self._append({"id": e.id, "op": "intend", "kind": kind, "service": service, "ref": ref, "ts": e.ts, "extra": extra})
        self.entries[e.id] = e
        return e

    def done(self, e: Entry, ref: str = "") -> None:
        if ref:
            e.ref = ref
        e.state = "done"
        self._append({"id": e.id, "op": "done", "ref": e.ref})

    def undone(self, e: Entry) -> None:
        self._append({"id": e.id, "op": "undone"})
        self.entries.pop(e.id, None)

    def live(self, kind: str | None = None, service: str | None = None) -> list[Entry]:
        return [
            e for e in self.entries.values() if (kind is None or e.kind == kind) and (service is None or e.service == service)
        ]

    def owns(self, kind: str, ref: str) -> bool:
        return any(e.kind == kind and e.ref == ref for e in self.entries.values())
