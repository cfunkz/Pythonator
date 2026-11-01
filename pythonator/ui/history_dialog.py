"""Search a service's full on-disk log history (including rotated files) off the GUI thread."""

from __future__ import annotations

from collections import deque

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.logs import LogStore
from . import theme

RESULT_LIMIT = 20_000


class _Signals(QObject):
    done = pyqtSignal(int, list, int)


class _SearchJob(QRunnable):
    def __init__(self, request: int, store: LogStore, query: str, case: bool) -> None:
        super().__init__()
        self.request, self.store, self.query, self.case = request, store, query, case
        self.signals = _Signals()

    def run(self) -> None:
        if self.query:
            lines, total = self.store.search(self.query, limit=RESULT_LIMIT, case_sensitive=self.case)
        else:
            tail: deque[str] = deque(maxlen=RESULT_LIMIT)
            total = 0
            for line in self.store.iter_file_lines():
                tail.append(line)
                total += 1
            lines = list(tail)
        self.signals.done.emit(self.request, lines, total)


class HistoryDialog(QDialog):
    def __init__(self, title: str, store: LogStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Log history — {title}")
        self.resize(1000, 640)
        self._store = store
        self._request = 0

        self.query = QLineEdit()
        self.query.setPlaceholderText("Search the whole log history (Enter). Empty shows the latest lines.")
        self.case = QCheckBox("Match case")
        go = QPushButton("Search")
        go.setProperty("accent", True)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(theme.monospace_font(10))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setUndoRedoEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(self.case)
        top.addWidget(go)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        files = ", ".join(p.name for p in store.files()) or "no log file yet"
        foot = QLabel(f"Files: {files}")
        foot.setObjectName("Dim")
        foot.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom = QHBoxLayout()
        bottom.addWidget(foot, 1)
        bottom.addWidget(self.status)
        layout.addLayout(bottom)

        self.query.returnPressed.connect(self.run_search)
        go.clicked.connect(self.run_search)
        self.run_search()

    def run_search(self) -> None:
        self._request += 1
        self.status.setText("Searching…")
        job = _SearchJob(self._request, self._store, self.query.text(), self.case.isChecked())
        job.signals.done.connect(self._show)
        QThreadPool.globalInstance().start(job)  # type: ignore[union-attr]

    def _show(self, request: int, lines: list[str], total: int) -> None:
        if request != self._request:
            return
        self.view.setPlainText("\n".join(lines))
        q = self.query.text()
        if q:
            shown = f" (showing newest {len(lines):,})" if total > len(lines) else ""
            self.status.setText(f"{total:,} matches{shown}")
            self._highlight(q)
        else:
            self.status.setText(f"{total:,} lines" + (f" (showing last {len(lines):,})" if total > len(lines) else ""))
        self.view.moveCursor(QTextCursor.MoveOperation.End)

    def _highlight(self, needle: str) -> None:
        doc = self.view.document()
        if doc is None:
            return
        fmt = QTextCharFormat()
        color = QColor(theme.YELLOW)
        color.setAlpha(90)
        fmt.setBackground(color)
        flags = QTextDocument.FindFlag.FindCaseSensitively if self.case.isChecked() else QTextDocument.FindFlag(0)
        cursor = QTextCursor(doc)
        count = 0
        while count < 5000 and not (cursor := doc.find(needle, cursor, flags)).isNull():
            cursor.mergeCharFormat(fmt)
            count += 1
