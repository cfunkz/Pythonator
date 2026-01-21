"""Log buffer - Ring buffer with timestamps and file persistence."""
from __future__ import annotations
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional
from config import LOGS_DIR, MAX_LOG_LINES, HISTORY_CHUNK, normalize, strip_ansi

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
        if not text: return "", ""
        text = normalize(text)
        data = self._partial + text
        self._partial = ""
        if "\n" not in data: self._partial = data; return "", ""
        
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = data.splitlines(keepends=True)
        if parts and not parts[-1].endswith("\n"): self._partial = parts.pop()
        
        display, file_out = [], []
        for part in parts:
            content = part.rstrip("\n")
            disp = f"[\x1b[94m{ts}\x1b[0m] {content}\n"
            self.lines.append(disp)
            display.append(disp)
            file_out.append(f"[{ts}] {strip_ansi(content)}\n")
        
        self._cache = None
        try:
            with open(self.file, "a", encoding="utf-8", newline="\n") as f: f.write("".join(file_out))
        except: pass
        return "".join(display), "".join(file_out)

    def get_recent(self) -> str: return "".join(self.lines)

    def _read_file(self) -> list[str]:
        if not self.file.exists(): return [l.rstrip("\n") for l in self.lines]
        try:
            mtime = self.file.stat().st_mtime
            if self._cache and mtime == self._mtime: return self._cache
            self._cache = normalize(self.file.read_text(encoding="utf-8", errors="replace")).splitlines()
            self._mtime = mtime
            return self._cache
        except: return [l.rstrip("\n") for l in self.lines]

    def line_count(self) -> int: return len(self._read_file())

    def _colorize(self, line: str) -> str:
        if line.startswith("["):
            b = line.find("]")
            if b > 0: return f"[\x1b[94m{line[1:b]}\x1b[0m]{line[b+1:]}"
        return line

    def search(self, pattern: str) -> tuple[str, int]:
        p = pattern.lower()
        matches = [l for l in self._read_file() if p in l.lower()]
        return ("".join(f"{self._colorize(l)}\n" for l in matches), len(matches)) if matches else ("", 0)

    def load_chunk(self, end: int, size: int = HISTORY_CHUNK) -> tuple[str, int]:
        lines = self._read_file()
        if not lines or end <= 0: return "", 0
        start = max(0, end - size)
        chunk = lines[start:end]
        return ("".join(f"{self._colorize(l)}\n" for l in chunk), start) if chunk else ("", 0)

    def clear(self) -> None:
        self.lines.clear(); self._cache = None
        try: self.file.write_text("", encoding="utf-8")
        except: pass
