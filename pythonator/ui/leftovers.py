"""The window that asks before leftover project folders are deleted: the Project builder's Refresh found them (folders
in the projects folder no service uses any more). One window for all of them; the user ticks which go."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .widgets import label


class LeftoversDialog(QDialog):
    """``delete`` is called with the ticked folders when the user presses Delete; nothing happens otherwise."""

    def __init__(self, plugin: str, folders: list[str], delete: Callable[[list[str]], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_delete = delete
        self.setWindowTitle("Delete leftover projects")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.addWidget(label(f"{plugin or 'A plugin'} found {len(folders)} project folder"
                               f"{'s' if len(folders) != 1 else ''} no service uses any more. Delete the ticked ones?",
                               plain=True))
        self.list = QListWidget()
        for folder in folders:
            item = QListWidgetItem(folder)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setToolTip(folder)
            self.list.addItem(item)
        self.list.itemChanged.connect(self._update)
        layout.addWidget(self.list)
        layout.addWidget(label("Everything in a ticked folder is deleted for good. Untick a folder to keep it.",
                               muted=True))
        buttons = QDialogButtonBox()
        self.delete_button = QPushButton()
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.delete_button.clicked.connect(self._delete)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update()

    def checked(self) -> list[str]:
        return [item.text() for i in range(self.list.count())
                if (item := self.list.item(i)) is not None and item.checkState() == Qt.CheckState.Checked]

    def _update(self, *_args: object) -> None:
        n = len(self.checked())
        self.delete_button.setText(f"Delete {n} folder{'s' if n != 1 else ''}")
        self.delete_button.setEnabled(n > 0)

    def _delete(self) -> None:
        chosen = self.checked()
        if chosen:
            self.on_delete(chosen)
            self.accept()
