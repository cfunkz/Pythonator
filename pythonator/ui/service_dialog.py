"""Create / edit a service."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.launch import (
    LaunchError,
    build_env,
    build_launch,
    env_file_path,
    explicit_interpreter,
    format_env,
    load_env_file,
    parse_env,
    resolve_python,
    resolve_workdir,
)
from ..core.models import (
    NODE_EXTENSIONS,
    SCRIPT_EXTENSIONS,
    LaunchMode,
    RestartPolicy,
    Runtime,
    ServiceConfig,
    runtime_for_path,
)
from ..core.paths import IS_WINDOWS
from ..core.runtimes import find_program, runtime_info
from ..core.schedule import ScheduleError, parse_schedule, upcoming
from . import theme

_SCRIPT_FILTER = "Scripts and programs (" + " ".join(f"*{e}" for e in SCRIPT_EXTENSIONS) + ");;All files (*)"


def script_runtime(path: str, current: Runtime) -> Runtime:
    """The runtime for a chosen file, keeping Bun / Deno for JavaScript they also run."""
    detected = runtime_for_path(path)
    if path.lower().endswith(NODE_EXTENSIONS) and current in (Runtime.BUN, Runtime.DENO):
        return current
    return detected


def _interp_key(runtime: Runtime) -> str:
    return runtime.value if runtime in (Runtime.PYTHON, Runtime.NODE) else "other"


def _row(edit: QWidget, *buttons: QToolButton) -> QWidget:
    w = QWidget()
    layout = QHBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(edit, 1)
    for b in buttons:
        layout.addWidget(b)
    return w


def _button(text: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    return b


class ServiceDialog(QDialog):
    def __init__(self, config: ServiceConfig, taken_names: set[str], parent: QWidget | None = None,
                 *, is_new: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("New service" if is_new else f"Edit {config.name}")
        self.setMinimumWidth(640)
        self._original = config
        self._taken = taken_names - {config.name}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(200)
        self._preview_timer.timeout.connect(self._update_preview)

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._environment_tab(), "Environment")
        tabs.addTab(self._supervision_tab(), "Supervision")

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setFont(theme.monospace_font(9))
        self.preview.setStyleSheet(
            f"background:{theme.BG_ALT}; border:1px solid {theme.BORDER}; border-radius:6px; padding:8px;"
        )

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(QLabel("Command preview"))
        layout.addWidget(self.preview)
        layout.addWidget(self.buttons)

        self._load(config)
        for edit in (self.name, self.target, self.args, self.workdir, self.interp, self.runtime_args, self.env_file):
            edit.textChanged.connect(lambda _t: self._preview_timer.start())
        self.env.textChanged.connect(self._preview_timer.start)
        self.schedule.textChanged.connect(lambda _t: self._update_schedule_hint())
        self.mode.currentIndexChanged.connect(lambda _i: self._on_mode())
        self.runtime.currentIndexChanged.connect(lambda _i: self._on_runtime())
        self._on_runtime()
        self._update_preview()
        self._update_schedule_hint()

    # -- tabs -----------------------------------------------------------------------------

    def _general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.name = QLineEdit()
        self.runtime = QComboBox()
        for r in Runtime:
            self.runtime.addItem(r.label, r.value)
        self.mode = QComboBox()
        self.target = QLineEdit()
        self.btn_target = _button("…", "Browse for a script")
        self.btn_target.clicked.connect(self._browse_target)
        self.args = QLineEdit()
        self.args.setPlaceholderText("Arguments, e.g. --port 8000 --reload")
        self.workdir = QLineEdit()
        btn_wd = _button("…", "Choose a folder")
        btn_wd.clicked.connect(self._browse_workdir)
        self.target_label = QLabel("Script")
        form.addRow("Name", self.name)
        form.addRow("Runtime", self.runtime)
        form.addRow("Run", self.mode)
        form.addRow(self.target_label, _row(self.target, self.btn_target))
        form.addRow("Arguments", self.args)
        form.addRow("Working dir", _row(self.workdir, btn_wd))
        return w

    def _environment_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.interp = QLineEdit()
        btn_interp = _button("…", "Choose the interpreter / runtime executable")
        btn_interp.clicked.connect(self._browse_interpreter)
        self.interp_label = QLabel("Interpreter")
        self.runtime_args = QLineEdit()
        self.python_info = QLabel()
        self.python_info.setObjectName("Dim")
        self.python_info.setWordWrap(True)
        self.env = QPlainTextEdit()
        self.env.setPlaceholderText("KEY=value, one per line.  $VAR / ${VAR} expand.\nDISCORD_TOKEN=...\nPORT=8000")
        self.env.setFont(theme.monospace_font(10))
        self.env.setFixedHeight(110)
        self.env_file = QLineEdit()
        self.env_file.setPlaceholderText("e.g. .env  (relative to the working dir; variables below win)")
        btn_env = _button("…", "Choose an env file")
        btn_env.clicked.connect(self._browse_env_file)
        self.env_file_info = QLabel()
        self.env_file_info.setObjectName("Dim")
        self.env_file_info.setWordWrap(True)
        self.requirements = QPlainTextEdit()
        self.requirements.setFont(theme.monospace_font(10))
        self.requirements.setFixedHeight(110)
        self._interp_row = _row(self.interp, btn_interp)
        form.addRow(self.interp_label, self._interp_row)
        form.addRow("", self.python_info)
        form.addRow("Runtime options", self.runtime_args)
        form.addRow("Env file", _row(self.env_file, btn_env))
        form.addRow("", self.env_file_info)
        form.addRow("Variables", self.env)
        form.addRow("Packages", self.requirements)
        self._env_form = form
        return w

    def _supervision_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.restart = QComboBox()
        for p in RestartPolicy:
            self.restart.addItem(p.label, p.value)
        self.restart.currentIndexChanged.connect(lambda _i: self._update_schedule_hint())
        self.autostart = QCheckBox("Start automatically when Pythonator opens")
        self.stop_timeout = QDoubleSpinBox()
        self.stop_timeout.setRange(0.5, 600)
        self.stop_timeout.setSuffix(" s")
        self.stop_timeout.setDecimals(1)
        self.schedule = QLineEdit()
        self.schedule.setPlaceholderText("e.g.  every 15m   ·   09:00, 18:30   ·   */5 * * * *   ·   0 3 * * mon-fri")
        self.schedule_info = QLabel()
        self.schedule_info.setObjectName("Dim")
        self.schedule_info.setWordWrap(True)
        hint = QLabel(
            "Restarts back off exponentially (1s, 2s, 4s … 60s) while a service keeps crashing "
            "within 30 seconds of starting.\nStop sends "
            + ("CTRL+BREAK" if IS_WINDOWS else "SIGTERM")
            + " to the whole process tree, then kills it after the grace period."
        )
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        form.addRow("Restart", self.restart)
        form.addRow("Grace period", self.stop_timeout)
        form.addRow("", self.autostart)
        form.addRow("Schedule", self.schedule)
        form.addRow("", self.schedule_info)
        form.addRow("", hint)
        return w

    # -- data -----------------------------------------------------------------------------

    def _load(self, c: ServiceConfig) -> None:
        self.name.setText(c.name)
        self._interp_values = {"python": c.python, "node": c.node, "other": c.interpreter}
        self._interp_runtime = c.runtime
        self._mode_wanted = c.mode
        self.runtime.setCurrentIndex(self.runtime.findData(c.runtime.value))
        self.target.setText(c.target)
        self.args.setText(c.args)
        self.workdir.setText(c.workdir)
        self.interp.setText(explicit_interpreter(c))
        self.runtime_args.setText(c.runtime_args)
        self.env.setPlainText(format_env(c.env))
        self.env_file.setText(c.env_file)
        self.schedule.setText(c.schedule)
        self.requirements.setPlainText(c.requirements)
        self.restart.setCurrentIndex(self.restart.findData(c.restart.value))
        self.autostart.setChecked(c.autostart)
        self.stop_timeout.setValue(c.stop_timeout)

    def _current_runtime(self) -> Runtime:
        return Runtime(self.runtime.currentData() or Runtime.PYTHON.value)

    def _current_mode(self) -> LaunchMode:
        return LaunchMode(self.mode.currentData() or LaunchMode.SCRIPT.value)

    def result_config(self) -> ServiceConfig:
        values = dict(self._interp_values)
        values[_interp_key(self._current_runtime())] = self.interp.text().strip()
        return self._original.copy(
            name=self.name.text().strip(),
            runtime=self._current_runtime(),
            mode=self._current_mode(),
            target=self.target.text().strip(),
            args=self.args.text().strip(),
            workdir=self.workdir.text().strip(),
            python=values["python"],
            node=values["node"],
            interpreter=values["other"],
            runtime_args=self.runtime_args.text().strip(),
            env=parse_env(self.env.toPlainText()),
            env_file=self.env_file.text().strip(),
            schedule=" ".join(self.schedule.text().split()),
            requirements=self.requirements.toPlainText().strip(),
            restart=RestartPolicy(self.restart.currentData()),
            autostart=self.autostart.isChecked(),
            stop_timeout=self.stop_timeout.value(),
        )

    def _on_runtime(self) -> None:
        runtime = self._current_runtime()
        info = runtime_info(runtime)
        # Each runtime keeps its own interpreter path while switching back and forth.
        self._interp_values[_interp_key(self._interp_runtime)] = self.interp.text().strip()
        self._interp_runtime = runtime
        self.interp.blockSignals(True)
        self.interp.setText(self._interp_values[_interp_key(runtime)])
        self.interp.blockSignals(False)
        wanted = self._mode_wanted if self.mode.count() == 0 else self._current_mode()
        self.mode.blockSignals(True)
        self.mode.clear()
        for mode, mode_info in info.modes.items():
            self.mode.addItem(mode_info.label, mode.value)
        index = self.mode.findData(wanted.value)
        self.mode.setCurrentIndex(max(0, index))
        self.mode.blockSignals(False)

        has_program = bool(info.programs)
        self._env_form.setRowVisible(self._interp_row, has_program)
        self._env_form.setRowVisible(self.runtime_args, has_program)
        self.interp_label.setText(info.label if runtime in (Runtime.PYTHON, Runtime.NODE) else "Interpreter")
        if runtime is Runtime.PYTHON:
            self.interp.setPlaceholderText("Auto: .venv / venv in the working dir, else system Python")
        elif has_program:
            self.interp.setPlaceholderText(f"Auto: {info.programs[0]} from PATH")
        self.runtime_args.setPlaceholderText(info.options_hint or "Options for the runtime itself")
        self.requirements.setEnabled(info.install is not None or runtime is Runtime.PYTHON)
        self.requirements.setPlaceholderText(info.packages_hint or info.no_install)
        self._on_mode()

    def _on_mode(self) -> None:
        info = runtime_info(self._current_runtime())
        mode = self._current_mode()
        mode_info = info.modes.get(mode)
        if mode_info is not None:
            self.target_label.setText(mode_info.field)
            self.target.setPlaceholderText(mode_info.hint)
        self.btn_target.setVisible(mode is LaunchMode.SCRIPT)
        self.workdir.setPlaceholderText(
            "Default: the script's folder" if mode is LaunchMode.SCRIPT else f"Default: {Path.home()}"
        )
        self._preview_timer.start()

    def _update_preview(self) -> None:
        cfg = self.result_config()
        info = runtime_info(cfg.runtime)
        try:
            workdir = resolve_workdir(cfg)
            if cfg.runtime is Runtime.PYTHON:
                if cfg.mode is not LaunchMode.COMMAND or cfg.python:
                    python, venv = resolve_python(cfg, workdir)
                    self.python_info.setText(f"Using {python}" + (f"  (venv {venv})" if venv else "  (no venv)"))
                else:
                    self.python_info.setText("")
            elif info.programs:
                program = find_program(info, explicit_interpreter(cfg), build_env(cfg, None, None, workdir))
                self.python_info.setText(f"Using {program}")
            else:
                self.python_info.setText("")
        except LaunchError as exc:
            self.python_info.setText(str(exc))
        self._update_env_file_info(cfg)
        try:
            spec = build_launch(cfg)
            self.preview.setText(f"{spec.display()}\n\ncwd: {spec.cwd}")
        except LaunchError as exc:
            self.preview.setText(f"⚠ {exc}")

    def _update_env_file_info(self, cfg: ServiceConfig) -> None:
        workdir = resolve_workdir(cfg)
        path = env_file_path(cfg, workdir)
        if path is None:
            found = (workdir / ".env").is_file()
            self.env_file_info.setText("A .env file exists in the working dir; type .env to load it." if found else "")
            return
        try:
            names = list(load_env_file(cfg, workdir))
        except LaunchError as exc:
            self.env_file_info.setText(f"⚠ {exc}")
            return
        shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        self.env_file_info.setText(f"{len(names)} variable{'s' if len(names) != 1 else ''}: {shown}" if names
                                   else f"{path.name} has no variables.")

    def _update_schedule_hint(self) -> None:
        text = self.schedule.text()
        if not text.strip():
            self.schedule_info.setText("Starts the service on a timer, for jobs like backups or reports. "
                                       "Runs are skipped while the previous one is still going.")
            return
        try:
            schedule = parse_schedule(text)
        except ScheduleError as exc:
            self.schedule_info.setText(f"⚠ {exc}")
            return
        assert schedule is not None
        runs = ", ".join(t.strftime("%a %H:%M" if not t.second else "%a %H:%M:%S") for t in upcoming(schedule, 3))
        tip = "  Tip: set Restart to Never for jobs that finish." if self.restart.currentData() != "never" else ""
        self.schedule_info.setText(f"{schedule.describe().capitalize()}. Next: {runs}.{tip}")

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            self.name.setFocus()
            self.name.setPlaceholderText("A name is required")
            return
        if name in self._taken:
            self.name.setFocus()
            self.name.selectAll()
            self.preview.setText(f"⚠ A service called “{name}” already exists.")
            return
        try:
            parse_schedule(self.schedule.text())
        except ScheduleError as exc:
            self.schedule.setFocus()
            self.preview.setText(f"⚠ Schedule: {exc}")
            return
        self.accept()

    # -- browsing -------------------------------------------------------------------------

    def _browse_target(self) -> None:
        start = self.target.text() or self.workdir.text() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose script", start, _SCRIPT_FILTER)
        if path:
            self.target.setText(path)
            runtime = script_runtime(path, self._current_runtime())
            self.runtime.setCurrentIndex(self.runtime.findData(runtime.value))
            if not self.name.text().strip() or self.name.text() == self._original.name == "New service":
                self.name.setText(Path(path).stem)

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Working directory", self.workdir.text() or str(Path.home()))
        if path:
            self.workdir.setText(path)

    def _browse_env_file(self) -> None:
        start = str(resolve_workdir(self.result_config()))
        path, _ = QFileDialog.getOpenFileName(self, "Choose env file", start, "Env files (.env *.env);;All files (*)")
        if path:
            workdir = Path(start)
            chosen = Path(path)
            self.env_file.setText(chosen.relative_to(workdir).as_posix() if chosen.is_relative_to(workdir) else path)

    def _browse_interpreter(self) -> None:
        info = runtime_info(self._current_runtime())
        names = info.programs or ("*",)
        if IS_WINDOWS:
            filt = f"{info.label} ({' '.join(n + '*.exe' for n in names)});;All files (*)"
        else:
            filt = "All files (*)"
        start = self.interp.text() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, f"Choose {info.label}", start, filt)
        if path:
            self.interp.setText(path)
