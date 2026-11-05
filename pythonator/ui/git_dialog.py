"""From Git: clone a repository into a folder, then set it up as a service."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import git, validate
from . import theme
from .widgets import label, line, view


class _Signals(QObject):
    line = pyqtSignal(str)
    done = pyqtSignal(bool, str)


class GitDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("From Git")
        self.resize(620, 420)
        self.folder: Path | None = None
        self.url = line("", "https://github.com/owner/repo.git or git@github.com:owner/repo.git")
        self.branch = line("", "Branch or tag (empty: the default)")
        self.parent_dir = QLineEdit(str(Path.home() / "Pythonator"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.parent_dir, 1)
        row.addWidget(browse)
        self.output = view()
        self.output.setFont(theme.mono_font(9))
        self.error = label()
        self.error.setStyleSheet(f"color: {theme.color('error')}")
        form = QFormLayout()
        form.addRow("Repository", self.url)
        form.addRow("Branch", self.branch)
        form.addRow("Clone into", row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.clone = QPushButton("Clone")
        self.buttons.addButton(self.clone, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self._clone)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.output, 1)
        layout.addWidget(self.error)
        layout.addWidget(self.buttons)
        self.signals = _Signals()
        self.signals.line.connect(lambda text: self.output.appendPlainText(text.rstrip()))
        self.signals.done.connect(self._done)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Clone into", self.parent_dir.text())
        if folder:
            self.parent_dir.setText(folder)

    def _clone(self) -> None:
        try:
            url = validate.git_url(self.url.text())
            branch = validate.git_ref(self.branch.text())
            base = Path(validate.folder(self.parent_dir.text(), "The folder", must_exist=False))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git") or "repo"
        dest = base / name
        self.error.clear()
        self.clone.setEnabled(False)
        self.output.appendPlainText(f"Cloning into {dest}…")

        def work() -> None:
            try:
                base.mkdir(parents=True, exist_ok=True)
                git.clone(url, dest, branch, on_line=self.signals.line.emit)
                self.signals.done.emit(True, str(dest))
            except (git.GitError, OSError) as exc:
                self.signals.done.emit(False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, text: str) -> None:
        self.clone.setEnabled(True)
        if not ok:
            self.error.setText(text)
            return
        self.folder = Path(text)
        self.accept()
