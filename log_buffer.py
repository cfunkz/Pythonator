"""
Log buffer - Ring buffer with timestamps and file persistence.

NOTE: Log file writes are performed asynchronously on a background thread.
Qt delivers process output on the main (UI) thread; synchronous disk I/O here
can cause UI stalls/freeze during heavy output or when stopping many processes.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import queue
import threading
from typing import Optional

from config import LOGS_DIR, MAX_LOG_LINES, HISTORY_CHUNK, normalize, strip_ansi


class _AsyncFileWriter:
    """Non-blocking log file appender.

    UI thread enqueues writes; a worker thread does disk I/O.
    """

    def __init__(self, max_queue: int = 10000):
        self._q: "queue.Queue[tuple[Path, str]]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="log-writer", daemon=True)
        self._thread.start()

    def write(self, path: Path, text: str) -> None:
        """Enqueue text to append to a file. Never blocks the caller."""
        if not text:
            return
        try:
            self._q.put_nowait((path, text))
        except queue.Full:
            # Never block the UI thread. Drop the chunk and count it.
            self._dropped += 1

    def _run(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                path, text = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                path.parent.mkdir(exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(text)

                # If we dropped anything, record it once we successfully write again.
                if self._dropped:
                    dropped = self._dropped
                    self._dropped = 0
                    try:
                        with open(path, "a", encoding="utf-8", newline="\n") as f:
                            f.write(f"[log-writer] dropped {dropped} chunks due to backpressure\n")
                    except Exception:
                        pass
            except Exception:
                # Never crash the writer thread.
                pass
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def close(self, timeout_s: float = 2.0) -> None:
        """Request stop and wait briefly for draining."""
        self._stop.set()
        try:
            self._thread.join(timeout=timeout_s)
        except Exception:
            pass


_GLOBAL_WRITER: Optional[_AsyncFileWriter] = None


def _writer() -> _AsyncFileWriter:
    global _GLOBAL_WRITER
    if _GLOBAL_WRITER is None:
        _GLOBAL_WRITER = _AsyncFileWriter()
    return _GLOBAL_WRITER


def shutdown_log_writer() -> None:
    """Flush pending async log writes and stop the writer thread."""
    global _GLOBAL_WRITER
    if _GLOBAL_WRITER is None:
        return
    _GLOBAL_WRITER.close()
    _GLOBAL_WRITER = None


class LogBuffer:
    __slots__ = ("name", "lines", "file", "_cache", "_mtime", "_partial")

    def __init__(self, name: str):
        self.name = name
        self.lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        LOGS_DIR.mkdir(exist_ok=True)
        self.file = LOGS_DIR / f"{name}.log"
        self._cache: Optional[list[str]] = None
        self._mtime: float = 0
        self._partial: str = ""

    def append(self, text: str) -> tuple[str, str]:
        if not text:
            return "", ""

        text = normalize(text)
        data = self._partial + text
        self._partial = ""

        # No newline yet: keep buffering partial line
        if "\n" not in data:
            self._partial = data
            return "", ""

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = data.splitlines(keepends=True)

        # Keep trailing partial (no newline)
        if parts and not parts[-1].endswith("\n"):
            self._partial = parts.pop()

        display, file_out = [], []
        for part in parts:
            content = part.rstrip("\n")
            disp = f"[\x1b[94m{ts}\x1b[0m] {content}\n"
            self.lines.append(disp)
            display.append(disp)
            file_out.append(f"[{ts}] {strip_ansi(content)}\n")

        self._cache = None

        # Persist asynchronously to keep UI thread responsive.
        try:
            _writer().write(self.file, "".join(file_out))
        except Exception:
            pass

        return "".join(display), "".join(file_out)

    def get_recent(self) -> str:
        return "".join(self.lines)

    def _read_file(self) -> list[str]:
        if not self.file.exists():
            return [l.rstrip("\n") for l in self.lines]
        try:
            mtime = self.file.stat().st_mtime
            if self._cache and mtime == self._mtime:
                return self._cache
            self._cache = normalize(
                self.file.read_text(encoding="utf-8", errors="replace")
            ).splitlines()
            self._mtime = mtime
            return self._cache
        except Exception:
            return [l.rstrip("\n") for l in self.lines]

    def line_count(self) -> int:
        return len(self._read_file())

    def _colorize(self, line: str) -> str:
        if line.startswith("["):
            b = line.find("]")
            if b > 0:
                return f"[\x1b[94m{line[1:b]}\x1b[0m]{line[b+1:]}"
        return line

    def search(self, pattern: str) -> tuple[str, int]:
        p = pattern.lower()
        matches = [l for l in self._read_file() if p in l.lower()]
        if not matches:
            return "", 0
        return "".join(f"{self._colorize(l)}\n" for l in matches), len(matches)

    def load_chunk(self, end: int, size: int = HISTORY_CHUNK) -> tuple[str, int]:
        lines = self._read_file()
        if not lines or end <= 0:
            return "", 0
        start = max(0, end - size)
        chunk = lines[start:end]
        if not chunk:
            return "", 0
        return "".join(f"{self._colorize(l)}\n" for l in chunk), start

    def clear(self) -> None:
        self.lines.clear()
        self._cache = None
        try:
            self.file.write_text("", encoding="utf-8")
        except Exception:
            pass
