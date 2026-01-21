"""Log View - Per-bot log viewer with live/history/search modes."""
from __future__ import annotations
import os, subprocess, sys
from enum import Enum, auto
from typing import TYPE_CHECKING
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget, QFrame
from console import AnsiConsole
from log_buffer import LogBuffer
from config import MAX_FLUSH_CHARS, BTN
if TYPE_CHECKING: from stats import ProcessStats

class Mode(Enum):
    LIVE = auto(); HISTORY = auto(); SEARCH = auto()

class LogView(QWidget):
    def __init__(self, name: str, buffer: LogBuffer):
        super().__init__()
        self.name, self.buffer = name, buffer
        self._pending: list[str] = []
        self._mode = Mode.LIVE
        self._hist_start = self._hist_end = 0
        self._setup_ui()
        QTimer.singleShot(0, self._go_live)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        
        # Stats bar
        stats = QFrame()
        stats.setStyleSheet("QFrame { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 2px; }")
        sl = QHBoxLayout(stats); sl.setContentsMargins(8, 4, 8, 4)
        self.stats_label = QLabel("Stopped"); self.stats_label.setStyleSheet("color: #888; font-family: monospace;")
        sl.addWidget(self.stats_label); sl.addStretch()
        self.mode_label = QLabel(""); self.mode_label.setStyleSheet("color: #8f8; font-family: monospace;")
        sl.addWidget(self.mode_label)
        btn_open = QPushButton("📄"); btn_open.setFixedHeight(24); btn_open.clicked.connect(self._open_log)
        sl.addWidget(btn_open)
        layout.addWidget(stats)

        # Search row
        search = QHBoxLayout(); search.setSpacing(4)
        self.search_input = QLineEdit(); self.search_input.setPlaceholderText("Search history... (Enter)")
        self.search_input.setStyleSheet("QLineEdit { padding: 4px 8px; border: 1px solid #333; border-radius: 2px; background: #1a1a1a; } QLineEdit:focus { border-color: #4688d8; }")
        self.search_input.returnPressed.connect(self._do_search)
        self.btn_clear = QPushButton("✕"); self.btn_clear.setFixedWidth(28); self.btn_clear.clicked.connect(self._exit_search); self.btn_clear.setEnabled(False)
        self.search_label = QLabel(""); self.search_label.setStyleSheet("color: #888; font-size: 11px;")
        search.addWidget(self.search_input, 1); search.addWidget(self.btn_clear); search.addWidget(self.search_label)
        layout.addLayout(search)

        # Action buttons
        btns = QHBoxLayout(); btns.setSpacing(4)
        self.btn_older = QPushButton("▲ Older"); self.btn_older.clicked.connect(self._load_older)
        self.btn_live = QPushButton("▼ Live"); self.btn_live.clicked.connect(self._go_live)
        btn_clear = QPushButton("Clear"); btn_clear.clicked.connect(self._clear_view)
        btn_del = QPushButton("Delete"); btn_del.clicked.connect(self._clear_all)
        btn_end = QPushButton("↓ End"); btn_end.clicked.connect(self._scroll_end)
        self.line_info = QLabel(""); self.line_info.setStyleSheet("color: #888; font-size: 11px;")
        for b in (self.btn_older, self.btn_live, btn_clear, btn_del, btn_end): b.setStyleSheet(BTN)
        btns.addWidget(self.btn_older); btns.addWidget(self.btn_live); btns.addWidget(btn_clear)
        btns.addWidget(btn_del); btns.addWidget(btn_end); btns.addStretch(); btns.addWidget(self.line_info)
        layout.addLayout(btns)

        self.console = AnsiConsole(); layout.addWidget(self.console, 1)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search_input, self._exit_search)

    def _update_ui(self) -> None:
        total = self.buffer.line_count()
        if self._mode == Mode.LIVE:
            self.mode_label.setText("● LIVE"); self.mode_label.setStyleSheet("color: #8f8; font-family: monospace;")
            self.btn_older.setEnabled(total > 0); self.btn_live.setEnabled(False); self.btn_clear.setEnabled(False)
            self.line_info.setText(f"{len(self.buffer.lines):,} lines")
        elif self._mode == Mode.HISTORY:
            self.mode_label.setText("● HISTORY"); self.mode_label.setStyleSheet("color: #ff8; font-family: monospace;")
            self.btn_older.setEnabled(self._hist_start > 0); self.btn_live.setEnabled(True); self.btn_clear.setEnabled(False)
            if total: self.line_info.setText(f"Lines {self._hist_start+1:,} - {self._hist_end:,} of {total:,}")
        else:
            self.mode_label.setText("◎ SEARCH"); self.mode_label.setStyleSheet("color: #88f; font-family: monospace;")
            self.btn_older.setEnabled(False); self.btn_live.setEnabled(True); self.btn_clear.setEnabled(True)

    def update_stats(self, stats: "ProcessStats") -> None:
        color = "#8f8" if stats.running else "#888"
        self.stats_label.setText(str(stats)); self.stats_label.setStyleSheet(f"color: {color}; font-family: monospace;")

    def _open_log(self) -> None:
        path = str(self.buffer.file)
        try:
            if os.name == "nt": os.startfile(path)
            elif sys.platform == "darwin": subprocess.run(["open", path], check=False)
            else: subprocess.run(["xdg-open", path], check=False)
        except: pass

    def _go_live(self) -> None:
        self._mode = Mode.LIVE; self.search_input.clear(); self.search_label.setText("")
        self.console.set_content(self.buffer.get_recent()); self._scroll_end(); self._update_ui()

    def append(self, text: str) -> None: self._pending.append(text)

    def flush(self) -> None:
        if not self._pending: return
        text = "".join(self._pending); self._pending.clear()
        if self._mode != Mode.LIVE: return
        if len(text) > MAX_FLUSH_CHARS:
            nl = text.find('\n', len(text) - MAX_FLUSH_CHARS)
            text = text[nl+1:] if nl > 0 else text[-MAX_FLUSH_CHARS:]
        sb = self.console.verticalScrollBar(); at_bottom = sb.value() >= sb.maximum() - 50
        self.console.setUpdatesEnabled(False); self.console.append_text(text); self.console.setUpdatesEnabled(True)
        if at_bottom: sb.setValue(sb.maximum())
        self.line_info.setText(f"{len(self.buffer.lines):,} lines")

    def _load_older(self) -> None:
        if self._mode == Mode.SEARCH: return
        total = self.buffer.line_count()
        if not total: self.btn_older.setEnabled(False); return
        self.btn_older.setText("Loading..."); self.btn_older.setEnabled(False)
        self.console.setUpdatesEnabled(False)
        try:
            if self._mode == Mode.LIVE:
                self._mode = Mode.HISTORY; self._hist_end = total
                text, self._hist_start = self.buffer.load_chunk(total, 10000)
                self.console.set_content(text); self._scroll_end()
            else:
                if self._hist_start <= 0: return
                text, new_start = self.buffer.load_chunk(self._hist_start, 10000)
                if text:
                    sb = self.console.verticalScrollBar(); old_max, old_val = sb.maximum(), sb.value()
                    self.console.prepend_text(text); sb.setValue(old_val + (sb.maximum() - old_max))
                    self._hist_start = new_start
        finally:
            self.console.setUpdatesEnabled(True); self.btn_older.setText("▲ Older"); self._update_ui()

    def _do_search(self) -> None:
        query = self.search_input.text().strip()
        if not query: self._exit_search(); return
        self._mode = Mode.SEARCH; self.search_label.setText("Searching...")
        self.console.setUpdatesEnabled(False)
        try:
            results, count = self.buffer.search(query)
            self.console.set_content(results)
            self.search_label.setText(f"{count:,} matches" if count else "No matches")
            self.search_label.setStyleSheet(f"color: {'#8f8' if count else '#f88'}; font-size: 11px;")
        finally: self.console.setUpdatesEnabled(True); self._update_ui()

    def _exit_search(self) -> None:
        self.search_input.clear(); self.search_label.setText("")
        if self._mode == Mode.SEARCH: self._go_live()

    def _scroll_end(self) -> None:
        self.console.moveCursor(QTextCursor.MoveOperation.End)
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def _clear_view(self) -> None:
        self.console.clear(); self.console._reset(); self._mode = Mode.LIVE; self.search_label.setText(""); self._update_ui()

    def _clear_all(self) -> None: self.buffer.clear(); self._clear_view()
