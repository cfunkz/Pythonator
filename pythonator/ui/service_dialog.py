"""Add or edit a service: what runs, how isolated it is, who can reach it, its limits, and when it runs."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import envcheck, runtimes, validate
from ..core.models import Mode, Service
from ..paths import IS_WINDOWS
from . import theme
from .run_settings import LimitsTab, ScheduleTab
from .widgets import label, line, scroll


def _muted(text: str) -> QLabel:
    return label(text, muted=True)


class _Bridge(QObject):
    """Brings a worker thread's result back to the window's thread."""

    text = pyqtSignal(str)

    def send(self, text: str) -> None:
        """Emit ``text`` from the worker thread. The window may have closed meanwhile, deleting this object with
        it: then there is nobody left to tell, and the thread ends quietly instead of raising RuntimeError."""
        with contextlib.suppress(RuntimeError):
            self.text.emit(text)


class KeyValueTable(QTableWidget):
    def __init__(self, headers: tuple[str, str], parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(list(headers))
        header = self.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        vh = self.verticalHeader()
        if vh is not None:
            vh.hide()

        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self, self.delete_selected,
                  context=Qt.ShortcutContext.WidgetShortcut)

    def add(self, a: str = "", b: str = "", *, edit: bool = False) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setItem(row, 0, QTableWidgetItem(a))
        self.setItem(row, 1, QTableWidgetItem(b))
        if edit:  # a new, empty row: type its name straight away
            self.setCurrentCell(row, 0)
            if item := self.item(row, 0):
                self.editItem(item)

    def selected_rows(self) -> list[int]:
        rows = {i.row() for i in self.selectedIndexes()}
        if not rows and self.currentRow() >= 0 and self.currentItem() is not None:
            rows = {self.currentRow()}
        return sorted(rows)

    def delete_selected(self) -> None:
        rows = self.selected_rows()
        for row in reversed(rows):
            self.removeRow(row)
        if rows and self.rowCount():
            self.selectRow(min(rows[0], self.rowCount() - 1))

    def buttons(self, add_text: str, add: Callable[[], None] | None = None) -> QHBoxLayout:
        """A row of ＋ Add and − Delete under the table: Delete removes the selected rows (also the Delete key)."""
        row = QHBoxLayout()
        plus = QPushButton(f"＋ {add_text}")
        plus.setAutoDefault(False)
        plus.clicked.connect(lambda: add() if add else self.add(edit=True))
        minus = QPushButton("− Delete")
        minus.setAutoDefault(False)
        minus.setToolTip("Delete the selected rows (Delete)")
        minus.clicked.connect(self.delete_selected)
        minus.setEnabled(False)
        model = self.selectionModel()
        if model is not None:
            model.selectionChanged.connect(lambda *_: minus.setEnabled(bool(self.selected_rows())))
        row.addWidget(plus)
        row.addWidget(minus)
        row.addStretch(1)
        return row

    def pairs(self) -> list[tuple[str, str]]:
        out = []
        for r in range(self.rowCount()):
            a, b = self.item(r, 0), self.item(r, 1)
            key = a.text().strip() if a else ""
            if key:
                out.append((key, b.text() if b else ""))
        return out


class ServiceDialog(QDialog):
    def __init__(self, service: Service | None, capabilities: list[tuple[str, str]], parent: QWidget | None = None,
                 *, preset: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit service" if service else "New service")
        self.resize(760, 680)
        self.original = service
        self.result_service: Service | None = None
        data: dict[str, Any] = service.model_dump(mode="json") if service else {}
        data.update(preset or {})
        tabs = QTabWidget()
        tabs.addTab(self._general(data), "General")
        tabs.addTab(self._isolation(data, capabilities), "Isolation")
        tabs.addTab(self._network(data), "Network")
        tabs.addTab(self._limits(data), "Limits")
        tabs.addTab(self._schedule(data), "Schedule && restarts")
        tabs.addTab(self._environment(data), "Environment")
        self.error = label()
        self.error.setStyleSheet(f"color: {theme.color('error')}")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self._mode_changed()
        self._runtime_changed()

    # -- tabs ---------------------------------------------------------------------------------------------

    def _general(self, d: dict[str, Any]) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.name = line(d.get("name", ""), "My bot")
        self.folder = line(d.get("folder", ""), "The project folder")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_folder)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        self.runtime = QComboBox()
        for key, rt in runtimes.RUNTIMES.items():
            self.runtime.addItem(rt.label, key)
        self.runtime.addItem("Custom command", runtimes.CUSTOM)
        self.runtime.setCurrentIndex(max(0, self.runtime.findData(d.get("runtime", "python"))))
        self.runtime.currentIndexChanged.connect(self._runtime_changed)
        self.entry = line(d.get("entry", ""), "main.py, server.js, main.go, app.jar, compose.yaml…")
        pick = QPushButton("Choose…")
        pick.clicked.connect(self._browse_entry)
        erow = QHBoxLayout()
        erow.addWidget(self.entry, 1)
        erow.addWidget(pick)
        self.command = line(validate.join_command(d.get("command", [])), "e.g. uvicorn app:app --port 8000")
        self.args = line(validate.join_command(d.get("args", [])), "Arguments (optional)")
        self.own_env = QCheckBox("Own environment: its own venv, node_modules or package cache, never global installs")
        self.own_env.setToolTip("Start makes it if it's missing. Packages ▾ → Recreate environment starts it fresh.")
        self.own_env.setChecked(bool(d.get("own_env", True)))
        self.own_env.toggled.connect(self._env_changed)
        self.env_line = _muted("")
        self.hint = _muted("")
        detect = QPushButton("Detect from folder")
        detect.clicked.connect(self._detect)
        form.addRow("Name", self.name)
        form.addRow("Folder", row)
        form.addRow("Language", self.runtime)
        form.addRow("File to run", erow)
        form.addRow("Command", self.command)
        form.addRow("Arguments", self.args)
        form.addRow("", detect)
        form.addRow("", self.hint)
        form.addRow("Packages", self.own_env)
        form.addRow("", self.env_line)
        self.folder.textChanged.connect(self._env_changed)
        return w

    def _isolation(self, d: dict[str, Any], caps: list[tuple[str, str]]) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.modes = QButtonGroup(self)
        mode = d.get("mode", "sandbox")
        texts = {
            "sandbox": ("Sandbox (recommended)", "Its own folder, its runtime and nothing else: your home folder, "
                                                 "keys, other projects and Pythonator's data are out of reach."),
            "container": ("Container", "Runs in Docker or Podman: only the project folder is mounted, all "
                                       "capabilities dropped, image read only."),
            "full": ("Full access", "Runs as you, like in a terminal. Only for code you trust."),
        }
        for key, (title, text) in texts.items():
            radio = QRadioButton(title)
            radio.setProperty("mode", key)
            radio.setChecked(key == mode)
            self.modes.addButton(radio)
            layout.addWidget(radio)
            hint = _muted(text)
            hint.setContentsMargins(24, 0, 0, 6)
            layout.addWidget(hint)
        self.modes.buttonToggled.connect(lambda *_: self._mode_changed())
        sb = d.get("sandbox", {})
        self.sandbox_box = QGroupBox("Sandbox")
        sl = QVBoxLayout(self.sandbox_box)
        self.write_project = QCheckBox("May change files in its folder")
        self.write_project.setChecked(sb.get("write_project", True))
        self.folders = KeyValueTable(("More folders", "Write (yes/no)"))
        for g in sb.get("folders", []):
            self.folders.add(g["path"], "yes" if g.get("write") else "no")
        sl.addWidget(self.write_project)
        sl.addWidget(self.folders)
        sl.addLayout(self.folders.buttons("Add folder…", self._add_folder))
        if caps:
            lines = "\n".join(f"{'✓' if v == 'enforced' else '•'} {k}: {v}" for k, v in caps)
            sl.addWidget(_muted(f"On this computer:\n{lines}"))
        ct = d.get("container", {})
        self.container_box = QGroupBox("Container")
        cf = QFormLayout(self.container_box)
        self.engine = QComboBox()
        self.engine.addItems(["auto", "podman", "docker"])
        self.engine.setCurrentText(ct.get("engine", "auto"))
        self.image = line(ct.get("image", ""), "Empty: picked by language, e.g. python:3.13-slim")
        self.cnet = line(ct.get("network", ""), "Join a container network (to reach a database)")
        self.ro_root = QCheckBox("Image read only (only /app, /tmp and home are writable)")
        self.ro_root.setChecked(ct.get("read_only_root", True))
        cf.addRow("Engine", self.engine)
        cf.addRow("Image", self.image)
        cf.addRow("Network", self.cnet)
        cf.addRow("", self.ro_root)
        layout.addWidget(self.sandbox_box)
        layout.addWidget(self.container_box)
        layout.addStretch(1)
        return w

    def _network(self, d: dict[str, Any]) -> QWidget:
        n = d.get("network", {})
        w = QWidget()
        form = QFormLayout(w)
        self.net_on = QCheckBox("May use the network")
        self.net_on.setChecked(n.get("enabled", True))
        self.ports = line(", ".join(map(str, n.get("ports", []))), "e.g. 8000, 8443")
        self.expose = QComboBox()
        self.expose.addItem("This computer only (your browser, other programs here)", "local")
        self.expose.addItem("Other computers on home or work networks too", "lan")
        self.expose.setCurrentIndex(0 if n.get("expose", "local") == "local" else 1)
        form.addRow("", self.net_on)
        form.addRow("Serves on ports", self.ports)
        form.addRow("Who may connect", self.expose)
        form.addRow("", _muted("Sandboxed services may listen only on these ports (Linux 6.7+, macOS, containers). "
                               "PORT and HOST are set for the program unless you set them."))
        if IS_WINDOWS:
            self.reach_note = _muted("Windows keeps sandboxed servers unreachable, even from this PC, until you "
                                     "allow it once (administrator prompt).")
            reach = QPushButton("Make reachable…")
            undo = QPushButton("Undo")
            reach.clicked.connect(lambda: self._reach(False))
            undo.clicked.connect(lambda: self._reach(True))
            row = QHBoxLayout()
            row.addWidget(reach)
            row.addWidget(undo)
            row.addStretch(1)
            form.addRow("Windows", row)
            form.addRow("", self.reach_note)
        return w

    def _limits(self, d: dict[str, Any]) -> QWidget:
        self.limits_tab = LimitsTab(d)
        self.memory, self.cpu = self.limits_tab.memory.edit, self.limits_tab.cpu.edit
        self.memory_note, self.cpu_note = self.limits_tab.memory.note, self.limits_tab.cpu.note
        area = scroll()
        area.setWidget(self.limits_tab)
        return area

    def limit_values(self) -> tuple[int, int]:
        """(memory_mb, cpu_percent) from the Limits tab. Raises ValueError with what to write instead."""
        return self.limits_tab.values()

    def _schedule(self, d: dict[str, Any]) -> QWidget:
        tab = self.schedule_tab = ScheduleTab(d)
        self.schedule, self.autostart = tab.schedule, tab.autostart
        self.restart, self.max_restarts, self.stop_timeout = tab.restart, tab.max_restarts, tab.stop_timeout
        area = scroll()
        area.setWidget(tab)
        return area

    def _environment(self, d: dict[str, Any]) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.env = KeyValueTable(("Variable", "Value"))
        for k, v in d.get("env", {}).items():
            self.env.add(k, v)
        load = QPushButton("Import from env file")
        load.setAutoDefault(False)
        load.setToolTip("Copy the variables of the env file below into this table (existing names are kept)")
        load.clicked.connect(self._import_env_file)
        self.env_file = line(d.get("env_file", ""), ".env (optional)")
        g = d.get("git") or {}
        self.git_url = line(g.get("url", ""), "https://github.com/owner/repo.git (optional)")
        self.git_branch = line(g.get("branch", ""), "Branch (empty: default)")
        self.git_pull = QCheckBox("Pull the newest code before every start (fast-forward only)")
        self.git_pull.setChecked(bool(g.get("pull_on_start")))
        layout.addWidget(self.env, 1)
        buttons = self.env.buttons("Add variable")
        buttons.addWidget(load)
        layout.addLayout(buttons)
        self.env_note = _muted("")
        layout.addWidget(self.env_note)
        layout.addWidget(_muted("Values of variables named like *TOKEN*, *KEY*, *SECRET* or *PASSWORD* are hidden "
                                "in the console and logs."))
        form = QFormLayout()
        form.addRow("Env file", self.env_file)
        form.addRow("Git", self.git_url)
        form.addRow("Branch", self.git_branch)
        form.addRow("", self.git_pull)
        layout.addLayout(form)
        return w

    def _import_env_file(self) -> None:
        name = self.env_file.text().strip() or ".env"
        path = Path(self.folder.text().strip() or ".") / name
        if not path.is_file():
            self.error.setText(f"There is no {name} in the folder")
            return
        from ..isolation.base import read_env_file

        have = {k for k, _ in self.env.pairs()}
        added = [(k, v) for k, v in read_env_file(path.parent, path.name).items() if k not in have]
        for key, value in added:
            self.env.add(key, value)
        self.error.setText("")
        self.env_note.setText(f"Imported {len(added)} variable{'s' if len(added) != 1 else ''} from {name}.")

    # -- behaviour ----------------------------------------------------------------------------------------

    def mode(self) -> str:
        checked = self.modes.checkedButton()
        return str(checked.property("mode")) if checked else "sandbox"

    def _mode_changed(self) -> None:
        mode = self.mode()
        self.sandbox_box.setVisible(mode == "sandbox")
        self.container_box.setVisible(mode == "container")
        self._env_changed()

    def _runtime_changed(self) -> None:
        custom = self.runtime.currentData() == runtimes.CUSTOM
        if self.runtime.currentData() == runtimes.COMPOSE and self.mode() != "container":
            for b in self.modes.buttons():  # Compose projects run in Container mode only
                if b.property("mode") == "container":
                    b.setChecked(True)
        self.command.setEnabled(custom)
        self.entry.setEnabled(not custom)
        key = self.runtime.currentData()
        rt = runtimes.RUNTIMES.get(key) if isinstance(key, str) else None
        if rt is not None:
            found = next((p for name in rt.programs if (p := runtimes.which(name))), None)
            where = f"Found {found}" if found else f"{rt.label} isn't installed ({' / '.join(rt.programs)})"
            self.hint.setText(f"{where}.")
        else:
            self.hint.setText("The command runs as is (no shell), in the folder above.")
        self._env_changed()

    def _env_changed(self) -> None:
        if not hasattr(self, "env_line") or not hasattr(self, "modes"):
            return  # still being built
        key = str(self.runtime.currentData())
        container = self.mode() == "container"
        applies = key in runtimes.OWN_ENV and not container
        self.own_env.setEnabled(applies)
        if key == runtimes.CUSTOM:
            self.env_line.setText("A custom command manages its own packages.")
            return
        self.env_line.setText(runtimes.env_summary(key, Path(self.folder.text().strip() or "."),
                                                   own=self.own_env.isChecked(), container=container))

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Project folder", self.folder.text())
        if folder:
            self.folder.setText(folder)
            if not self.name.text():
                self.name.setText(Path(folder).name[:64])
            if not self.entry.text():
                self._detect()

    def _browse_entry(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "File to run", self.folder.text())
        if not path:
            return
        p = Path(path)
        if not self.folder.text():
            self.folder.setText(str(p.parent))
        try:
            self.entry.setText(validate.relative_file(path, self.folder.text()))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        if (rt := runtimes.for_entry(path)) is not None:
            self.runtime.setCurrentIndex(self.runtime.findData(rt.key))

    def _detect(self) -> None:
        folder = Path(self.folder.text())
        if not folder.is_dir():
            return
        rt, entry = runtimes.detect(folder)
        if rt is not None:
            self.runtime.setCurrentIndex(self.runtime.findData(rt.key))
            self.entry.setText(entry)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder the sandbox may use")
        if folder:
            self.folders.add(folder, "no")

    def _reach(self, undo: bool) -> None:
        if self.original is None:
            self.error.setText("Save the service first.")
            return
        from ..isolation import windows

        try:
            ports = validate.parse_ports(self.ports.text())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        reserved = [p for p in ports if not undo and envcheck.port_reserved(p)]
        if reserved:  # nothing can listen there: an administrator prompt for it would only add rules for nothing
            self.error.setText(f"Windows has reserved port {reserved[0]} for itself (Hyper-V, WSL or Docker). "
                               "Change the port first, then Make reachable.")
            return
        lan = self.expose.currentData() == "lan"
        sid = self.original.id
        self.reach_note.setText("Waiting for the administrator prompt…")
        bridge = _Bridge(self)
        bridge.text.connect(self.reach_note.setText)

        def work() -> None:
            ok, message = windows.make_reachable(sid, ports, lan=lan, undo=undo)
            bridge.send(("✓ " if ok else "✗ ") + message)

        threading.Thread(target=work, daemon=True).start()

    def build(self) -> Service:
        runtime = str(self.runtime.currentData())
        memory_mb, cpu_percent = self.limit_values()
        data: dict[str, Any] = {
            "name": self.name.text(), "folder": self.folder.text().strip(), "runtime": runtime,
            "entry": self.entry.text().strip() if runtime != runtimes.CUSTOM else "",
            "command": validate.split_command(self.command.text()) if runtime == runtimes.CUSTOM else [],
            "args": validate.split_command(self.args.text()), "env": dict(self.env.pairs()),
            "env_file": self.env_file.text().strip(), "mode": self.mode(), "own_env": self.own_env.isChecked(),
            "network": {"enabled": self.net_on.isChecked(), "ports": validate.parse_ports(self.ports.text()),
                        "expose": self.expose.currentData()},
            "sandbox": {"write_project": self.write_project.isChecked(),
                        "folders": [{"path": p, "write": w.strip().lower() in ("yes", "y", "rw", "write", "1")}
                                    for p, w in self.folders.pairs()]},
            "container": {"engine": self.engine.currentText(), "image": self.image.text(),
                          "network": self.cnet.text(), "read_only_root": self.ro_root.isChecked()},
            "restart": self.restart.value(), "max_restarts": self.max_restarts.value(),
            "stop_timeout": self.stop_timeout.value(), "memory_mb": memory_mb, "cpu_percent": cpu_percent,
            "autostart": self.autostart.isChecked(), "schedule": self.schedule_tab.schedule_text(),
            "git": {"url": self.git_url.text(), "branch": self.git_branch.text(),
                    "pull_on_start": self.git_pull.isChecked()} if self.git_url.text().strip() else None,
        }
        if self.original is not None:
            data["id"] = self.original.id
            data["full_access_ok"] = self.original.full_access_ok and self.mode() == Mode.FULL.value
        if not Path(data["folder"]).is_dir():
            raise ValueError("The folder doesn't exist")
        return Service.model_validate(data)

    def _save(self) -> None:
        try:
            self.result_service = self.build()
        except ValidationError as exc:
            err = exc.errors()[0]
            where = ".".join(str(x) for x in err["loc"])
            self.error.setText(f"{where}: {err['msg']}".removeprefix(": ").replace("Value error, ", ""))
            return
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()

