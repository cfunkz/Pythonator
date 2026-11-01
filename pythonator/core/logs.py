"""Per-service log storage.

A :class:`LogStore` turns raw process output into timestamped lines, keeps the most recent
ones in memory for the console, and appends everything to a size-rotated log file. Output
is assembled and written on the process reader threads, so the GUI thread only ever picks
up finished :class:`LogLine` objects.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import IO

from .ansi import strip_ansi
from .safety import open_private_append
from .urls import address_url, local_url

STALE_PARTIAL_SECONDS = 0.25  # show an unterminated line (e.g. an input() prompt) after this long
MAX_LINE_CHARS = 32_768  # longer lines (minified JSON, binary junk) are split so nothing balloons
ADDRESS_HINT_LINES = 5_000  # look for "listening on :8080"-style addresses in this many lines after a start


class LineKind(IntEnum):
    OUT = 0  # process output
    SYSTEM = 1  # supervisor message (started, exited, ...)
    ERROR = 2  # supervisor error
    INPUT = 3  # text sent to the process' stdin, echoed


@dataclass(frozen=True, slots=True)
class LogLine:
    ts: float
    text: str
    kind: LineKind = LineKind.OUT


_FILE_PREFIX = {
    LineKind.OUT: "",
    LineKind.SYSTEM: "[pythonator] ",
    LineKind.ERROR: "[pythonator] ERROR: ",
    LineKind.INPUT: "> ",
}


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_LINE_CHARS:
        return [text]
    return [text[i : i + MAX_LINE_CHARS] for i in range(0, len(text), MAX_LINE_CHARS)]


def _clean_line(raw: str) -> str:
    """Emulate a terminal's carriage return: keep only what was written after the last ``\\r``."""
    if raw.endswith("\r"):
        raw = raw.rstrip("\r")
    cr = raw.rfind("\r")
    return raw[cr + 1 :] if cr >= 0 else raw


class LogStream:
    """One producer (a process) writing into a :class:`LogStore`; owns its partial line."""

    __slots__ = ("_partial", "_since", "_store")

    def __init__(self, store: LogStore) -> None:
        self._store = store
        self._partial = ""
        self._since = 0.0

    def write(self, chunk: str) -> None:
        if not chunk:
            return
        if "\x00" in chunk:
            chunk = chunk.replace("\x00", "")
        store = self._store
        with store._lock:
            data = self._partial + chunk
            parts = data.split("\n")
            tail = parts.pop()
            full = [piece for p in parts for piece in _split_long(_clean_line(p))]
            if len(tail) > MAX_LINE_CHARS:
                # Output without newlines would otherwise grow forever (and get slower to append to).
                pieces = _split_long(_clean_line(tail))
                tail = pieces.pop() if len(pieces[-1]) < MAX_LINE_CHARS else ""
                full.extend(pieces)
            if tail and (full or not self._partial):
                self._since = time.monotonic()
            self._partial = tail
            if not full:
                return
            now = time.time()
            lines = [LogLine(now, text) for text in full]
            store._push(lines)
        store._write_file(lines)
        if not store.url_exact:
            store._find_url(lines)

    def _flush_stale(self, now: float) -> LogLine | None:
        # Caller holds the store lock.
        if self._partial and now - self._since >= STALE_PARTIAL_SECONDS:
            line = LogLine(time.time(), _clean_line(self._partial))
            self._partial = ""
            return line
        return None

    def close(self) -> None:
        store = self._store
        lines: list[LogLine] = []
        with store._lock:
            if self._partial:
                lines = [LogLine(time.time(), _clean_line(self._partial))]
                store._push(lines)
                self._partial = ""
            with contextlib.suppress(ValueError):
                store._streams.remove(self)
        store._write_file(lines)


class LogStore:
    """Thread-safe ring buffer + rotating log file for one service."""

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int = 10_000,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._path = path
        self._max_bytes = max_bytes
        self._backups = backups
        self._ring: deque[LogLine] = deque(maxlen=max_lines)
        self._pending: deque[LogLine] = deque(maxlen=max_lines)
        self._dropped = 0
        self._streams: list[LogStream] = []
        self._fh: IO[str] | None = None
        self._size = 0
        self._ts_second = -1
        self._ts_text = ""
        self.total_lines = 0
        self.url: str | None = None  # the address a web server printed since reset_url(), if any
        self.url_exact = False  # True once ``url`` is a full printed link, not a "port 3000" guess
        self._hint_budget = ADDRESS_HINT_LINES
        self.file_error: str | None = None  # why the log file can't be written (disk full, ...), if so

    # -- configuration --------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def configure(self, *, max_lines: int, max_bytes: int, backups: int) -> None:
        with self._lock:
            if max_lines != self._ring.maxlen:
                self._ring = deque(self._ring, maxlen=max_lines)
                self._pending = deque(self._pending, maxlen=max_lines)
        with self._file_lock:
            self._max_bytes, self._backups = max_bytes, backups

    def relocate(self, path: Path) -> None:
        """Move the log (and rotated backups) to ``path``, e.g. after a rename."""
        if path == self._path:
            return
        with self._file_lock:
            self._close_file()
            for old in self._files_unlocked():
                suffix = old.name[len(self._path.name) :]
                with contextlib.suppress(OSError):
                    old.replace(path.with_name(path.name + suffix))
            self._path = path

    def reset_url(self) -> None:
        self.url = None
        self.url_exact = False
        self._hint_budget = ADDRESS_HINT_LINES

    def _find_url(self, lines: list[LogLine]) -> None:
        """Remember the first full local link; until one shows up, also accept a bare address.

        A full link ("http://127.0.0.1:5000/docs") always wins over an earlier "port 5000" guess.
        Bare addresses are only looked for shortly after a start (servers print them at startup),
        which keeps this cheap for bots that log forever.
        """
        for line in lines:
            text = line.text
            if "://" in text and (url := local_url(text)):
                self.url, self.url_exact = url, True
                return
            if self.url is None and self._hint_budget > 0:
                self._hint_budget -= 1
                if url := address_url(text):
                    self.url = url

    # -- producers ------------------------------------------------------------------------

    def stream(self) -> LogStream:
        s = LogStream(self)
        with self._lock:
            self._streams.append(s)
        return s

    def system(self, message: str, kind: LineKind = LineKind.SYSTEM) -> None:
        now = time.time()
        lines = [LogLine(now, line, kind) for line in message.split("\n")]
        with self._lock:
            self._push(lines)
        self._write_file(lines)

    def _push(self, lines: list[LogLine]) -> None:
        # Caller holds ``self._lock`` and calls ``_write_file`` after releasing it, so disk I/O
        # never stalls the GUI thread waiting in ``drain``.
        if not lines:
            return
        if len(self._pending) + len(lines) > (self._pending.maxlen or 0):
            self._dropped += len(self._pending) + len(lines) - (self._pending.maxlen or 0)
        self._ring.extend(lines)
        self._pending.extend(lines)
        self.total_lines += len(lines)

    # -- consumers ------------------------------------------------------------------------

    def drain(self) -> list[LogLine]:
        """Return lines produced since the previous call (GUI thread, ~10x per second)."""
        now = time.monotonic()
        with self._lock:
            stale = [ln for s in self._streams if (ln := s._flush_stale(now)) is not None]
            if stale:
                self._push(stale)
            out = list(self._pending)
            self._pending.clear()
            dropped, self._dropped = self._dropped, 0
        if stale:
            self._write_file(stale)
            if not self.url_exact:
                self._find_url(stale)
        if not out:
            return []
        if dropped:
            note = LogLine(out[0].ts, f"… {dropped:,} lines skipped in the console (see the log file)",
                           LineKind.SYSTEM)
            out.insert(0, note)
        return out

    def last_output(self) -> str:
        """The program's newest output, including a prompt still waiting for its newline."""
        with self._lock:
            for stream in reversed(self._streams):
                if stream._partial:
                    return stream._partial
            for line in reversed(self._ring):
                if line.kind is LineKind.OUT:
                    return line.text
                if line.kind is not LineKind.INPUT:
                    break
        return ""

    def snapshot(self) -> list[LogLine]:
        with self._lock:
            self._pending.clear()
            self._dropped = 0
            return list(self._ring)

    def clear(self, *, delete_files: bool = False) -> None:
        with self._lock:
            self._ring.clear()
            self._pending.clear()
            self._dropped = 0
        if delete_files:
            with self._file_lock:
                self._close_file()
                for f in self._files_unlocked():
                    with contextlib.suppress(OSError):
                        f.unlink()
                self._size = 0

    # -- files ----------------------------------------------------------------------------

    def files(self) -> list[Path]:
        """Existing log files, oldest first."""
        with self._file_lock:
            if self._fh:
                with contextlib.suppress(OSError, ValueError):
                    self._fh.flush()
            return self._files_unlocked()

    def _files_unlocked(self) -> list[Path]:
        backups = sorted(
            (p for p in self._path.parent.glob(self._path.name + ".*") if p.suffix[1:].isdigit()),
            key=lambda p: int(p.suffix[1:]),
            reverse=True,
        )
        current = [self._path] if self._path.exists() else []
        return [*backups, *current]

    def iter_file_lines(self) -> Iterator[str]:
        for f in self.files():
            try:
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        yield line.rstrip("\n")
            except OSError:
                continue

    def search(self, query: str, *, limit: int = 5000, case_sensitive: bool = False) -> tuple[list[str], int]:
        """Search the full on-disk history. Returns the newest ``limit`` matches and the total count."""
        needle = query if case_sensitive else query.lower()
        found: deque[str] = deque(maxlen=limit)
        total = 0
        for line in self.iter_file_lines():
            if needle in (line if case_sensitive else line.lower()):
                found.append(line)
                total += 1
        return list(found), total

    def close(self) -> None:
        with self._file_lock:
            self._close_file()

    def _close_file(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError, ValueError):
                self._fh.close()
            self._fh = None

    def _timestamp(self, ts: float) -> str:
        sec = int(ts)
        if sec != self._ts_second:
            self._ts_second = sec
            self._ts_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
        return self._ts_text

    def _write_file(self, lines: list[LogLine]) -> None:
        if not lines:
            return
        with self._file_lock:
            out = "".join(
                f"[{self._timestamp(ln.ts)}] {_FILE_PREFIX[ln.kind]}{strip_ansi(ln.text)}\n" for ln in lines
            )
            try:
                if self._fh is None:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    # Owner-only: services print tokens and connection strings more often than you'd think.
                    self._fh = os.fdopen(open_private_append(self._path), "a", encoding="utf-8", newline="\n")
                    self._size = self._fh.tell()
                self._fh.write(out)
                self._fh.flush()
                self._size += len(out)
                if self._max_bytes and self._size >= self._max_bytes:
                    self._rotate()
                self.file_error = None
            except (OSError, ValueError) as exc:
                self._close_file()
                first = self.file_error is None
                self.file_error = getattr(exc, "strerror", None) or str(exc)
                if first:  # say it once in the console; output keeps flowing to memory
                    note = LogLine(time.time(), f"⚠ Could not write the log file {self._path}: {self.file_error}. "
                                   "The console keeps working.", LineKind.ERROR)
                    with self._lock:
                        self._push([note])

    def _rotate(self) -> None:
        self._close_file()
        try:
            if self._backups <= 0:
                self._path.unlink(missing_ok=True)
            else:
                oldest = self._path.with_name(f"{self._path.name}.{self._backups}")
                oldest.unlink(missing_ok=True)
                for i in range(self._backups - 1, 0, -1):
                    src = self._path.with_name(f"{self._path.name}.{i}")
                    if src.exists():
                        src.replace(self._path.with_name(f"{self._path.name}.{i + 1}"))
                self._path.replace(self._path.with_name(f"{self._path.name}.1"))
        except OSError:
            pass  # e.g. another program holds the file open on Windows; try again later
        self._size = 0
