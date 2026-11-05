"""Settings: how Start all and sign-in start services, log rotation, the tray and starting with the computer.
The services' order itself is the service list's (move them with Alt+↑ / Alt+↓ there)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
)

from ..core.models import Settings
from ..paths import IS_WINDOWS
from . import login
from .widgets import label, spin

Request = Callable[..., None]
ONE, FEW, ALL = range(3)  # the start modes, as the combo box lists them


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, request: Request, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        self.settings = settings
        self.request = request

        self.mode = QComboBox()
        self.mode.addItems(["One after another (top to bottom)", "A few at a time", "All at once"])
        n = settings.start_at_once
        self.mode.setCurrentIndex(ALL if n == 0 else ONE if n == 1 else FEW)
        self.at_once = spin(2, 64, max(2, n or 4), suffix=" together")
        self.mode.currentIndexChanged.connect(lambda i: self.at_once.setEnabled(i == FEW))
        self.at_once.setEnabled(self.mode.currentIndex() == FEW)
        starting = QFormLayout()
        starting.addRow("Start services", self.mode)
        starting.addRow("", self.at_once)
        starting.addRow(label("Start all and sign-in use the service list's order: select services there and move "
                              "them with Alt+↑ / Alt+↓. The next one starts when the one before it has been running "
                              "for 2 s. All at once suits fast machines with few services.", muted=True))

        self.log_mb = spin(1, 1024, settings.log_max_mb, suffix=" MB")
        self.log_backups = spin(0, 50, settings.log_backups, suffix=" old logs")
        logs = QFormLayout()
        logs.addRow("Rotate a log at", self.log_mb)
        logs.addRow("Keep", self.log_backups)

        self.tray = QCheckBox("Keep Pythonator in the tray when the window is closed")
        self.tray.setChecked(settings.tray)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.setEnabled(False)
            self.tray.setToolTip("This desktop has no tray")
        self.login_was = login.enabled()
        self.login = QCheckBox("Start Pythonator with Windows" if IS_WINDOWS else "Start Pythonator when I sign in")
        self.login.setChecked(self.login_was)
        self.login.setToolTip("It opens in the tray (or minimised); services set to start with Pythonator start "
                              "then, in order")
        window = QVBoxLayout()
        window.addWidget(self.tray)
        window.addWidget(self.login)

        layout = QVBoxLayout(self)
        for title, inner in (("Starting services", starting), ("Logs", logs), ("Window", window)):
            box = QGroupBox(title)
            box.setLayout(inner)
            layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def start_at_once(self) -> int:
        return {ONE: 1, ALL: 0}.get(self.mode.currentIndex(), self.at_once.value())

    def save(self) -> None:
        self.settings = self.settings.model_copy(update={
            "start_at_once": self.start_at_once(), "log_max_mb": self.log_mb.value(),
            "log_backups": self.log_backups.value(), "tray": self.tray.isChecked()})
        self.request({"op": "settings", "settings": self.settings.model_dump(mode="json")})
        if self.login.isChecked() != self.login_was:
            try:
                login.set_enabled(self.login.isChecked())
            except OSError as exc:
                QMessageBox.warning(self, "Settings", f"Couldn't change starting with the computer: {exc}")
        self.accept()
