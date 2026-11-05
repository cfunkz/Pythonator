"""Settings: how Start all and login start services (how many at a time, in what order), and tidying up the
list: move services up and down, remove several at once."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core.models import Service, Settings
from .widgets import label, spin

Request = Callable[..., None]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, services: list[Service], request: Request, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.settings = settings
        self.request = request
        self.order = [s.id for s in services]

        form = QFormLayout()
        self.at_once = spin(1, 64, settings.start_at_once)
        self.at_once.setToolTip("1 starts them one after another")
        self.at_once.setMaximumWidth(110)
        form.addRow("Start at once", self.at_once)
        form.addRow("", label("<b>Start all</b> and login start services in the order below, this many together. "
                              "The next one starts as soon as one before it is up (running for 2 s), so a dozen "
                              "services don't all load at the same moment.", muted=True))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # Ctrl / Shift+click
        for s in services:
            item = QListWidgetItem(s.name)
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.list.addItem(item)
        up, down = QPushButton("Move up"), QPushButton("Move down")
        self.remove = QPushButton("Remove…")
        self.remove.setProperty("danger", True)
        up.clicked.connect(lambda: self.shift(-1))
        down.clicked.connect(lambda: self.shift(1))
        self.remove.clicked.connect(self.remove_selected)
        side = QVBoxLayout()
        for b in (up, down):
            side.addWidget(b)
        side.addStretch(1)
        side.addWidget(self.remove)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(side)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(label("Services, in start order (select several with Ctrl or Shift)", muted=True))
        layout.addLayout(row, 1)
        layout.addWidget(buttons)

    def ids(self) -> list[str]:
        items = (self.list.item(i) for i in range(self.list.count()))
        return [str(item.data(Qt.ItemDataRole.UserRole)) for item in items if item is not None]

    def selected_rows(self) -> list[int]:
        return sorted(self.list.row(item) for item in self.list.selectedItems())

    def shift(self, step: int) -> None:
        """Move the selected services one place up or down, together."""
        rows = self.selected_rows()
        if not rows or (step < 0 and rows[0] == 0) or (step > 0 and rows[-1] == self.list.count() - 1):
            return
        for r in rows if step < 0 else reversed(rows):
            item = self.list.takeItem(r)
            self.list.insertItem(r + step, item)
        self.list.clearSelection()
        for r in rows:
            if (item := self.list.item(r + step)) is not None:
                item.setSelected(True)

    def remove_selected(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        names = ", ".join(i.text() for i in items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")
        if QMessageBox.question(self, "Remove", f"Remove {len(items)} service(s): {names}? Their files stay where "
                                "they are; their logs and sandbox folders are deleted.") \
                != QMessageBox.StandardButton.Yes:
            return
        for item in items:
            self.request({"op": "remove", "id": str(item.data(Qt.ItemDataRole.UserRole))})
            self.list.takeItem(self.list.row(item))

    def save(self) -> None:
        self.settings = self.settings.model_copy(update={"start_at_once": self.at_once.value()})
        self.request({"op": "settings", "settings": self.settings.model_dump(mode="json")})
        if self.ids() != [i for i in self.order if i in self.ids()]:
            self.request({"op": "reorder", "ids": self.ids()})
        self.accept()
