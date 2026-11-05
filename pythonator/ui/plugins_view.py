"""The Plugins window: turn plugins on and off, set them up, grant permissions, and install third-party ones."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..plugins.manifest import PERMISSIONS
from . import theme
from .widgets import label, scroll, spin, view

Send = Callable[..., None]  # (message, reply=None)


def third_party_warning(parent: QWidget, plugin: dict[str, Any]) -> bool:
    """The warning before a third-party plugin runs. True if the user goes ahead."""
    perms = "\n".join(f"  • {PERMISSIONS.get(p, p)}" for p in plugin.get("permissions", [])) or "  (none)"
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Third-party plugin")
    box.setText(f"{plugin['name']} {plugin['version']} isn't an official Pythonator plugin.")
    box.setInformativeText(
        "Only run plugins from people you trust.\n\n"
        f"From: {plugin['folder']}\nSHA-256: {plugin['sha256']}\n\n"
        f"It asks to:\n{perms}\n\n"
        "It runs in a sandbox and can't see your files (with read_files, it can ask to read a service's files, "
        "read-only and never its secrets). It starts in test mode: nothing it asks for is granted, "
        "and actions it tries are only recorded, until you grant permissions and leave test mode.")
    run = box.addButton("Enable in test mode", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is run


class PluginsDialog(QDialog):
    def __init__(self, send: Send, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plugins")
        self.resize(900, 620)
        self.send = send
        self.plugins: dict[str, dict[str, Any]] = {}
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self._show())
        install_dir = QPushButton("Install from folder…")
        install_dir.clicked.connect(self._install_folder)
        install_git = QPushButton("Install from Git…")
        install_git.clicked.connect(self._install_git)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.list, 1)
        ll.addWidget(install_dir)
        ll.addWidget(install_git)
        self.detail = scroll()
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.detail)
        split.setSizes([260, 640])
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        self._fields: dict[str, Any] = {}

    def refresh(self, plugins: list[dict[str, Any]]) -> None:
        current = self.list.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole) if current else None
        before = _comparable(self.plugins.get(keep)) if keep else None
        self.plugins = {p["id"]: p for p in plugins}
        self.list.blockSignals(True)
        self.list.clear()
        for p in plugins:
            state = "on" if p["running"] else ("error" if p["error"] else "off")
            tier = "official" if p["official"] else ("third-party · test mode" if p["test_mode"] else "third-party")
            item = QListWidgetItem(f"{p['name']}\n{tier} · {state}")
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            self.list.addItem(item)
            if p["id"] == keep:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        if keep is not None and keep == self._fields.get("__id") and before == _comparable(self.plugins.get(keep)):
            return  # the shown plugin didn't change: keep its page, and whatever is being typed in it
        self._show()

    def append_activity(self, plugin: str, text: str) -> None:
        if plugin in self.plugins:
            self.plugins[plugin].setdefault("activity", []).append(text)
        log = self._fields.get("__activity")
        if isinstance(log, QPlainTextEdit) and self._fields.get("__id") == plugin:
            log.appendPlainText(text)

    def _selected(self) -> dict[str, Any] | None:
        item = self.list.currentItem()
        return self.plugins.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _show(self) -> None:
        p = self._selected()
        body = QWidget()
        layout = QVBoxLayout(body)
        self._fields = {}
        if p is None:
            layout.addWidget(QLabel("No plugins."))
            self.detail.setWidget(body)
            return
        self._fields["__id"] = p["id"]
        title = QLabel(f"{p['name']}  {p['version']}")
        title.setProperty("heading", True)
        badge = QLabel("Official · verified" if p["official"] else "Third-party · sandboxed")
        badge.setProperty("badge", True)
        badge.setStyleSheet(f"color: {theme.color('ok' if p['official'] else 'warn')}")
        head = QHBoxLayout()
        head.addWidget(title)
        head.addWidget(badge)
        head.addStretch(1)
        layout.addLayout(head)
        desc = label(p["description"])
        desc.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(desc)
        info = label(f"{p['folder']}\nSHA-256 {p['sha256'][:16]}…")
        info.setProperty("muted", True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)
        if p["error"]:
            err = label(p["error"])
            err.setTextFormat(Qt.TextFormat.PlainText)
            err.setStyleSheet(f"color: {theme.color('error')}")
            layout.addWidget(err)
        enabled = QCheckBox("Enabled")
        enabled.setChecked(p["enabled"])
        enabled.toggled.connect(lambda on: self._enable(p, on))
        layout.addWidget(enabled)
        layout.addWidget(self._permissions(p))
        if p["settings"]:
            layout.addWidget(self._settings(p))
        log = view("\n".join(p.get("activity", [])))
        log.setFont(theme.mono_font(9))
        log.setMinimumHeight(140)
        self._fields["__activity"] = log
        box = QGroupBox("Activity")
        QVBoxLayout(box).addWidget(log)
        layout.addWidget(box)
        if not p["official"]:
            remove = QPushButton("Remove plugin")
            remove.setProperty("danger", True)
            remove.clicked.connect(lambda: self._remove(p))
            layout.addWidget(remove)
        layout.addStretch(1)
        self.detail.setWidget(body)

    def _permissions(self, p: dict[str, Any]) -> QGroupBox:
        box = QGroupBox("Permissions")
        layout = QVBoxLayout(box)
        checks: dict[str, QCheckBox] = {}
        for perm in p["permissions"]:
            check = QCheckBox(PERMISSIONS.get(perm, perm))
            check.setChecked(perm in p["granted"])
            check.setEnabled(not p["official"])
            checks[perm] = check
            layout.addWidget(check)
        if not p["permissions"]:
            layout.addWidget(QLabel("It asks for nothing."))
        if p["official"]:
            note = QLabel("Official plugins get what they declare. They still run in a sandbox of their own.")
        else:
            test = QCheckBox("Test mode: record what it tries to do instead of doing it")
            test.setChecked(p["test_mode"])
            layout.addWidget(test)
            apply = QPushButton("Apply")
            apply.clicked.connect(lambda: self.send({"op": "plugin_grant", "plugin": p["id"],
                                                     "permissions": [k for k, c in checks.items() if c.isChecked()],
                                                     "live": not test.isChecked()}))
            layout.addWidget(apply)
            note = QLabel("Changes restart the plugin.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        layout.addWidget(note)
        return box

    def _settings(self, p: dict[str, Any]) -> QGroupBox:
        box = QGroupBox("Settings")
        form = QFormLayout(box)
        widgets: dict[str, Any] = {}
        for s in p["settings"]:
            value = p["values"].get(s["key"], s["default"])
            kind = s["kind"]
            w: QWidget
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(value))
            elif kind == "int":
                w = spin(0, 1_000_000, int(value or 0))
            elif kind == "choice":
                w = QComboBox()
                w.addItems(s["choices"])
                w.setCurrentText(str(value))
            elif kind == "lines":
                w = QPlainTextEdit(str(value))
                w.setMaximumHeight(110)
            else:
                w = QLineEdit(str(value))
                if kind == "secret":
                    w.setEchoMode(QLineEdit.EchoMode.Password)
            if s.get("help"):
                w.setToolTip(s["help"])
            widgets[s["key"]] = w
            form.addRow(s["label"], w)
            if s.get("help"):
                hint = label(s["help"])
                hint.setTextFormat(Qt.TextFormat.PlainText)
                hint.setProperty("muted", True)
                form.addRow("", hint)
            if kind == "secret":
                where = p.get("secrets_stored", {}).get(s["key"], "")
                text = {"keychain": "Saved in your system keychain, not in Pythonator's settings.",
                        "file": "No system keychain here: saved in a private file only you can read.",
                        "settings": "Saved in an older place: save it again to move it to the keychain."}.get(
                    where, "Saved in your system keychain when you save it.")
                hint = label(text, muted=True)
                form.addRow("", hint)
        save = QPushButton("Save settings")
        save.setProperty("primary", True)
        save.clicked.connect(lambda: self.send({"op": "plugin_settings", "plugin": p["id"],
                                                "values": {k: _value(w) for k, w in widgets.items()}}))
        form.addRow("", save)
        return box

    def _enable(self, p: dict[str, Any], on: bool) -> None:
        if on and not p["official"] and not third_party_warning(self, p):
            self.refresh(list(self.plugins.values()))
            return
        self.send({"op": "plugin_enable", "plugin": p["id"], "enabled": on})

    def _remove(self, p: dict[str, Any]) -> None:
        if QMessageBox.question(self, "Remove plugin", f"Remove {p['name']} and its data?") \
                == QMessageBox.StandardButton.Yes:
            self.send({"op": "plugin_remove", "plugin": p["id"]})

    def _install(self, source: str) -> None:
        def done(ok: bool, data: Any, error: str) -> None:
            if not ok:
                QMessageBox.warning(self, "Not installed", error)
            else:
                QMessageBox.information(self, "Installed", f"Installed {data}. It is off until you enable it.")

        self.send({"op": "plugin_install", "source": source}, done)

    def _install_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Plugin folder (with plugin.toml)")
        if folder:
            self._install(folder)

    def _install_git(self) -> None:
        url, ok = QInputDialog.getText(self, "Install from Git", "https:// address of the plugin's repository:")
        if ok and url.strip():
            self._install(url.strip())


def _value(w: Any) -> str | int | bool:
    if isinstance(w, QCheckBox):
        return w.isChecked()
    if isinstance(w, QSpinBox):
        return w.value()
    if isinstance(w, QComboBox):
        return w.currentText()
    if isinstance(w, QPlainTextEdit):
        return w.toPlainText()
    return str(w.text())


def _comparable(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the detail page shows, minus the activity log (that is appended in place)."""
    return None if p is None else {k: v for k, v in p.items() if k != "activity"}
