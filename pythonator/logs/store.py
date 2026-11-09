"""Log store: segment files with a sparse line index, so any range of lines is one seek away.
A 10-million-line log costs the same to show as a 100-line one: the window asks for lines [a, a+n) and gets
exactly those. Every line is masked before it is written, so secrets never reach the disk.
Line format on disk: "<unix ms> <stream> <text>\n", stream is o (stdout), e (stderr) or s (Pythonator)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from pythonator.safe.mask import Masker
from pythonator.store.files import ensure_private_dir

SEGMENT_BYTES = 4 * 1024 * 1024
KEEP_SEGMENTS = 16  # 64 MB per service
STRIDE = 64  # one index entry every STRIDE lines
MAX_LINE = 16 * 1024

@dataclass(frozen=True)
class Line:
    n: int  # global line number (0 = first line ever written)
    ts: int  # unix ms
    stream: str  # o, e, s
    text: str

_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no \n -> \r\n translation, so byte offsets stay true

class _Segment:
    def __init__(self, path: Path, first: int) -> None:
        self.path = path
        self.first = first
        self.count = 0
        self.size = 0
        self.index: list[int] = []  # byte offset of line first + i*STRIDE

    def scan(self) -> None:
        self.index.clear()
        self.count = 0
        off = 0
        with open(self.path, "rb") as f:
            for raw in f:
                if self.count % STRIDE == 0:
                    self.index.append(off)
                off += len(raw)
                self.count += 1
        self.size = off

class LogStore:
    def __init__(self, root: Path, name: str, masker: Masker | None = None) -> None:
        self.dir = ensure_private_dir(root / name)
        self.masker = masker or Masker()
        self.segments: list[_Segment] = []
        self._fd: int | None = None
        self._open()

    def _open(self) -> None:
        segs = sorted(self.dir.glob("seg-*.log"))
        first = 0
        for p in segs:
            try:
                first = int(p.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            s = _Segment(p, first)
            s.scan()
            self.segments.append(s)
        if not self.segments:
            self._new_segment(0)

    def _new_segment(self, first: int) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        p = self.dir / f"seg-{first:012d}.log"
        s = _Segment(p, first)
        self.segments.append(s)
        while len(self.segments) > KEEP_SEGMENTS:
            old = self.segments.pop(0)
            old.path.unlink(missing_ok=True)

    def _writer(self) -> int:
        if self._fd is None:
            self._fd = os.open(self.segments[-1].path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY, 0o600)
        return self._fd

    def set_masker(self, masker: Masker) -> None:
        self.masker = masker

    @property
    def first(self) -> int:
        return self.segments[0].first

    @property
    def total(self) -> int:
        s = self.segments[-1]
        return s.first + s.count

    def append(self, stream: str, text: str, ts: int | None = None) -> int:
        """Append one line (masked). Returns its line number."""
        text = self.masker.mask(text.replace("\r", "").replace("\n", " "))
        if len(text) > MAX_LINE:
            text = text[:MAX_LINE] + " …[cut]"
        raw = f"{ts or int(time.time() * 1000)} {stream} {text}\n".encode("utf-8", errors="replace")
        seg = self.segments[-1]
        if seg.size + len(raw) > SEGMENT_BYTES and seg.count:
            self._new_segment(seg.first + seg.count)
            seg = self.segments[-1]
        if seg.count % STRIDE == 0:
            seg.index.append(seg.size)
        os.write(self._writer(), raw)
        seg.size += len(raw)
        seg.count += 1
        return seg.first + seg.count - 1

    def read(self, start: int, count: int) -> list[Line]:
        """Lines [start, start+count). Lines that rotated away are skipped."""
        out: list[Line] = []
        start = max(start, self.first)
        end = min(start + max(0, min(count, 5000)), self.total)
        n = start
        for seg in self.segments:
            if n >= end:
                break
            if not (seg.first <= n < seg.first + seg.count):
                continue
            rel = n - seg.first
            k = rel // STRIDE
            with open(seg.path, "rb") as f:
                f.seek(seg.index[k])
                i = k * STRIDE
                for raw in f:
                    if i >= rel:
                        if n >= end:
                            break
                        out.append(_parse(n, raw))
                        n += 1
                    i += 1
                    if i >= seg.count:
                        break
        return out

    def tail(self, count: int) -> list[Line]:
        return self.read(max(self.first, self.total - count), count)

    def search(self, needle: str, limit: int = 200) -> list[Line]:
        """Newest-first substring search; stops after `limit` hits."""
        hits: list[Line] = []
        low = needle.lower()
        for seg in reversed(self.segments):
            with open(seg.path, "rb") as f:
                lines = f.readlines()
            for i in range(len(lines) - 1, -1, -1):
                if low in lines[i].decode("utf-8", "replace").lower():
                    hits.append(_parse(seg.first + i, lines[i]))
                    if len(hits) >= limit:
                        return hits
        return hits

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

def _parse(n: int, raw: bytes) -> Line:
    s = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    ts, _, rest = s.partition(" ")
    stream, _, text = rest.partition(" ")
    try:
        t = int(ts)
    except ValueError:
        t = 0
    return Line(n, t, stream or "o", text)
