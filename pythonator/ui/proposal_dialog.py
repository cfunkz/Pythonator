"""The review window for a change a plugin suggests (``propose_changes``): the diff, the warnings, Apply or Reject.

Pythonator writes the file only when the user presses Apply here, and only if the file hasn't changed since. The
diff is shown as plain text, coloured by line; nothing in it is interpreted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import info, label, warn

Send = Callable[..., None]


class ProposalDialog(QDialog):
    def __init__(self, proposal: dict[str, Any], send: Send, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.proposal = proposal
        self.send = send
        self.done_ = False
        self.setWindowTitle(f"Review a change: {proposal.get('path', '')}")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        head = QLabel(f"{proposal.get('plugin_name', 'A plugin')} suggests "
                      f"{'a new file' if proposal.get('new_file') else 'a change to'} {proposal.get('path', '')} "
                      f"in {proposal.get('service_name', '')}")
        head.setTextFormat(Qt.TextFormat.PlainText)  # the path and names come from the plugin
        head.setProperty("heading", True)
        head.setWordWrap(True)
        layout.addWidget(head)
        if proposal.get("summary"):
            layout.addWidget(label(str(proposal["summary"]), plain=True))
        where = label(f"Folder: {proposal.get('folder', '')}", plain=True, muted=True)
        layout.addWidget(where)
        for warning in proposal.get("warnings", []):
            w = label(f"⚠ {warning}", plain=True)
            w.setStyleSheet(f"color: {theme.color('warn')}")
            layout.addWidget(w)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(theme.mono_font(9))
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._fill(str(proposal.get("diff", "")))
        layout.addWidget(self.diff, 1)
        note = label("Nothing is written unless you press Apply. Pythonator keeps a copy of the old file, and refuses "
                     "if the file changed since. Read the whole change: it comes from an AI or a plugin.", muted=True)
        layout.addWidget(note)
        buttons = QDialogButtonBox()
        self.apply_button = QPushButton("Apply change")
        self.apply_button.setProperty("primary", True)
        reject = QPushButton("Reject")
        buttons.addButton(self.apply_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(reject, QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_button.clicked.connect(lambda: self._answer(True))
        reject.clicked.connect(lambda: self._answer(False))
        reject.setDefault(True)  # Enter never applies by accident
        layout.addWidget(buttons)

    def _fill(self, text: str) -> None:
        cursor = self.diff.textCursor()
        colors = {"+": theme.color("ok"), "-": theme.color("error"), "@": theme.color("muted")}
        for line in text.splitlines():
            fmt = QTextCharFormat()
            key = line[:1] if not line.startswith(("+++", "---")) else "@"
            if key in colors:
                fmt.setForeground(QColor(colors[key]))
            cursor.insertText(line + "\n", fmt)
        self.diff.moveCursor(QTextCursor.MoveOperation.Start)

    def _answer(self, apply: bool) -> None:
        if self.done_:
            return
        self.apply_button.setEnabled(False)

        def done(ok: bool, data: Any, error: str) -> None:
            self.done_ = True
            if not ok:
                warn(self, "Not applied", error)
            elif apply:
                info(self, "Applied", str(data or "Saved"))
            self.accept()

        self.send({"op": "plugin_proposal", "plugin": self.proposal["plugin"], "proposal": self.proposal["id"],
                   "apply": apply}, done)

    def reject(self) -> None:  # Esc or the close button: the same as Reject
        if not self.done_:
            self._answer(False)
            return
        super().reject()

    def closed_elsewhere(self) -> None:
        """Answered in another window, or expired."""
        self.done_ = True
        super().reject()
