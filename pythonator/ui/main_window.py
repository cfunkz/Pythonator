"""Main window: services sidebar, code editor and live console side by side."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QByteArray, QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.launch import LaunchError, resolve_python, resolve_workdir
from ..core.login import LoginItem
from ..core.models import (
    SCRIPT_EXTENSIONS,
    CloseAction,
    ConsoleShell,
    LaunchMode,
    Runtime,
    ServiceConfig,
    new_id,
    runtime_for_path,
)
from ..core.runtimes import runtime_info
from ..core.safety import risky_command
from ..core.shells import SHELL_LABELS, resolve_shell, shell_choices
from ..core.starters import Starter, starter_for
from ..supervisor import Service, Status, Supervisor, format_next_run, format_uptime
from . import keys, theme
from .console import ConsolePanel
from .editor_panel import EditorPanel
from .file_tree import FileTree
from .history_dialog import HistoryDialog
from .prompts import confirm_install, confirm_risky_command
from .service_dialog import ServiceDialog
from .service_list import ServiceDelegate, ServiceListModel, ServiceRole, describe_target

FLUSH_MS = 80
STATS_MS = 1000
# Where the tray icon lives, in the platform's words.
TRAY = "menu bar" if keys.IS_MAC else "system tray"


def open_web_url(url: str) -> bool:
    """Open a link from program output in the browser: only web links, never files or app schemes."""
    qurl = QUrl(url, QUrl.ParsingMode.StrictMode)
    if not qurl.isValid() or qurl.scheme().lower() not in ("http", "https") or not qurl.host():
        return False
    return QDesktopServices.openUrl(qurl)


def _tool(text: str, tip: str = "", *, accent: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    if accent:
        b.setStyleSheet(f"QToolButton {{ background: {theme.ACCENT}; color: white; border-color: {theme.ACCENT}; }}"
                        "QToolButton:hover { background: #6aa0fa; } QToolButton:disabled { background: #2a3444;"
                        " color: #6b727c; border-color: #2a3444; }")
    return b


class SettingsDialog(QDialog):
    def __init__(self, sup: Supervisor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        st = sup.config.settings
        self.lines = QSpinBox()
        self.lines.setRange(500, 1_000_000)
        self.lines.setSingleStep(1000)
        self.lines.setValue(st.console_lines)
        self.log_size = QSpinBox()
        self.log_size.setRange(1, 10_000)
        self.log_size.setSuffix(" MB")
        self.log_size.setValue(st.log_max_mb)
        self.backups = QSpinBox()
        self.backups.setRange(0, 100)
        self.backups.setValue(st.log_backups)
        self.close_action = QComboBox()
        for action, label in (
            (CloseAction.ASK, "Ask"),
            (CloseAction.TRAY, "Keep services running in the tray"),
            (CloseAction.QUIT, "Stop services and quit"),
        ):
            self.close_action.addItem(label, action.value)
        self.close_action.setCurrentIndex(self.close_action.findData(st.close_action.value))
        self.console_shell = QComboBox()
        for shell in shell_choices():
            label = SHELL_LABELS[shell]
            if shell is ConsoleShell.AUTO:
                label += f" ({resolve_shell(shell).name})"
            self.console_shell.addItem(label, shell.value)
        self.console_shell.setCurrentIndex(max(0, self.console_shell.findData(st.console_shell.value)))
        self.console_shell.setToolTip("The shell that runs commands typed in the console's $ mode.\n"
                                      "PowerShell understands mv, ls, rm, cp and cat; cmd doesn't.")
        form = QFormLayout()
        form.addRow("Console lines per service", self.lines)
        form.addRow("Rotate log files at", self.log_size)
        form.addRow("Rotated files to keep", self.backups)
        form.addRow("When closing the window", self.close_action)
        form.addRow("Console $ shell", self.console_shell)
        self.login_item = LoginItem()
        self.start_at_login = QCheckBox("Start Pythonator when I log in")
        self.start_at_login.setToolTip(
            f"Opens Pythonator in the {TRAY} after you log in, and starts the services set to "
            f"“Start automatically”.\nSet up in {self.login_item.where()}")
        self.start_at_login.setChecked(self.login_item.supported and self.login_item.is_enabled())
        self.start_at_login.setEnabled(self.login_item.supported)
        form.addRow("", self.start_at_login)
        info = QLabel(f"Data folder: {sup.paths.root}")
        info.setObjectName("Dim")
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(info)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, supervisor: Supervisor) -> None:
        super().__init__()
        self.sup = supervisor
        self._current: str | None = None
        self._quitting = False
        self._system_quit = False  # the platform asked the app to quit (⌘Q, logout): it quits by itself
        self.closed = False  # services stopped and state saved: the app may end
        self._tray_hint_shown = False
        self.setWindowTitle(f"Pythonator {__version__}")
        self.setWindowIcon(theme.app_icon())
        self.resize(1360, 860)
        self.setAcceptDrops(True)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._build_tray()
        self._bind_shortcuts()
        self._apply_settings_to_console()

        supervisor.changed.connect(self._on_service_changed)
        supervisor.services_changed.connect(self._on_services_changed)
        supervisor.notify.connect(self._on_notify)

        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start(FLUSH_MS)
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._tick)
        self._stats_timer.start(STATS_MS)

        self._restore_state()
        self._update_counts()
        if warning := supervisor.config_warning:
            self.statusBar().showMessage(warning)  # type: ignore[union-attr]
        if recovered := supervisor.recovered_config:
            self.statusBar().showMessage(  # type: ignore[union-attr]
                f"Your settings file was damaged; restored from {recovered}.", 15000)
        if self.model.rowCount():
            self.select(self.model.id_at(0))
        else:
            self._update_header()

    # -- construction ---------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        assert tb is not None
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        self.act_new = QAction("+  New service", self)
        self.act_new.setToolTip(f"Add a script, module or command to supervise ({keys.text(keys.NEW_SERVICE)})")
        self.act_new.triggered.connect(self.new_service)
        self.act_start_all = QAction("▶  Start all", self)
        self.act_start_all.triggered.connect(self.sup.start_all)
        self.act_stop_all = QAction("■  Stop all", self)
        self.act_stop_all.triggered.connect(self.sup.stop_all)
        self.act_settings = QAction("Settings", self)
        self.act_settings.triggered.connect(self.open_settings)
        # macOS: shown as "Settings…" (⌘,) in the application menu.
        self.act_settings.setMenuRole(QAction.MenuRole.PreferencesRole)
        tb.addAction(self.act_new)
        tb.addSeparator()
        tb.addAction(self.act_start_all)
        tb.addAction(self.act_stop_all)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_settings)

    def _build_body(self) -> None:
        # Sidebar
        sidebar = theme.named(QWidget(), "SidebarPane")
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 8, 0, 0)
        sl.setSpacing(4)
        title = theme.named(QLabel("SERVICES"), "SidebarTitle")
        title.setContentsMargins(14, 0, 0, 0)
        sl.addWidget(title)
        self.model = ServiceListModel(self.sup)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setItemDelegate(ServiceDelegate(self.sup, self.list))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.doubleClicked.connect(lambda _i: self.edit_service())
        if sel := self.list.selectionModel():
            sel.currentChanged.connect(self._on_list_current)
        sl.addWidget(self.list, 1)
        self.empty_hint = QLabel(
            "No services yet.\n\nClick “New service” or drop a script\n"
            "(.py, .js, .go, .jar, .rb, .php, .sh …)\nonto the window."
        )
        self.empty_hint.setObjectName("Dim")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self.empty_hint)

        self.files = FileTree()
        self.side_split = theme.named(QSplitter(Qt.Orientation.Vertical), "Sidebar")
        self.side_split.addWidget(sidebar)
        self.side_split.addWidget(self.files)
        self.side_split.setStretchFactor(0, 2)
        self.side_split.setStretchFactor(1, 3)
        self.side_split.setSizes([330, 470])
        self.side_split.setMinimumWidth(230)

        # Header
        header = theme.named(QWidget(), "Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 8, 10, 8)
        hl.setSpacing(6)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.h_name = theme.named(QLabel("—"), "HeaderTitle")
        self.h_info = theme.named(QLabel(""), "Dim")
        self.h_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        titles.addWidget(self.h_name)
        titles.addWidget(self.h_info)
        hl.addLayout(titles, 1)
        self.btn_start = _tool("▶  Start", f"Start ({keys.text(keys.START)})", accent=True)
        self.btn_stop = _tool("■  Stop", f"Stop gracefully; press again to kill ({keys.text(keys.STOP)})")
        self.btn_restart = _tool("↻  Restart", f"Restart ({keys.text(keys.START)} while running)")
        self.btn_edit = _tool("⚙  Configure", f"Edit service settings ({keys.text(keys.CONFIGURE)})")
        self.btn_web = _tool("🌐  Open", "Open the address this server printed in your browser")
        self.btn_web.setVisible(False)
        self.btn_web.clicked.connect(self._open_service_url)
        self.btn_deps = _tool("📦  Dependencies", "Create the venv, install packages, edit requirements.txt")
        self.btn_deps.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.deps_menu = QMenu(self)
        self.btn_deps.setMenu(self.deps_menu)
        self.deps_menu.aboutToShow.connect(self._fill_deps_menu)
        self.btn_more = _tool("⋯", "More actions")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self)
        self.btn_more.setMenu(self.more_menu)
        self.more_menu.aboutToShow.connect(lambda: self._fill_service_menu(self.more_menu, self.current()))
        for b in (self.btn_web, self.btn_start, self.btn_stop, self.btn_restart, self.btn_deps, self.btn_edit,
                  self.btn_more):
            hl.addWidget(b)
        self.btn_start.clicked.connect(lambda: self._with_current(self.start_service))
        self.btn_stop.clicked.connect(lambda: self._with_current(self.sup.stop))
        self.btn_restart.clicked.connect(lambda: self._with_current(self.restart_service))
        self.btn_edit.clicked.connect(self.edit_service)

        # Editor + console
        self.editor = EditorPanel()
        self.console = ConsolePanel()
        self.editor.status_message.connect(lambda m: self.statusBar().showMessage(m, max(5000, 60 * len(m))))  # type: ignore[union-attr]
        self.console.submitted.connect(self._on_console_submit)
        self.console.clear_requested.connect(self._clear_console)
        self.console.history_requested.connect(self._open_history)
        self.console.open_log_requested.connect(self._open_log)
        self.console.timestamps_toggled.connect(self._toggle_timestamps)
        self.console.wrap_toggled.connect(self._toggle_wrap)
        self.console.view.open_location.connect(self._open_location)
        self.console.view.open_url.connect(open_web_url)
        self.files.open_file.connect(lambda p: self.editor.open_file(p))
        self.files.run_file.connect(self.new_service)
        self.files.renamed.connect(self.editor.path_moved)
        self.files.status_message.connect(lambda m: self.statusBar().showMessage(m, 5000))  # type: ignore[union-attr]

        self.work_split = QSplitter(Qt.Orientation.Vertical)
        self.work_split.addWidget(self.editor)
        self.work_split.addWidget(self.console)
        self.work_split.setStretchFactor(0, 5)
        self.work_split.setStretchFactor(1, 4)
        self.work_split.setChildrenCollapsible(True)
        self.work_split.setSizes([480, 360])

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(header)
        rl.addWidget(self.work_split, 1)

        self.main_split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split.addWidget(self.side_split)
        self.main_split.addWidget(right)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setSizes([270, 1090])
        self.main_split.setCollapsible(1, False)
        self.setCentralWidget(self.main_split)

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        assert bar is not None
        self.sb_cursor = QLabel("")
        self.sb_running = QLabel("")
        bar.addPermanentWidget(self.sb_cursor)
        bar.addPermanentWidget(self.sb_running)
        self.editor.cursor_info.connect(self.sb_cursor.setText)

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(theme.app_icon(), self)
        menu = QMenu(self)
        show = menu.addAction("Show Pythonator")
        assert show is not None
        show.triggered.connect(self.show_window)
        menu.addSeparator()
        menu.addAction(self.act_start_all)
        menu.addAction(self.act_stop_all)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        assert quit_action is not None
        quit_action.triggered.connect(self.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.setToolTip("Pythonator")
        tray.show()
        self.tray = tray
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setQuitOnLastWindowClosed(False)

    def _bind_shortcuts(self) -> None:
        bindings: tuple[tuple[str | tuple[str, ...], Callable[[], object]], ...] = (
            (keys.START, lambda: self._with_current(self.start_or_restart)),
            (keys.STOP, lambda: self._with_current(self.sup.stop)),
            (keys.NEW_SERVICE, self.new_service),
            (keys.CONFIGURE, self.edit_service),
            # macOS: ⌘Q comes from the application menu as a quit request (see ui.app).
            *(() if keys.IS_MAC else ((keys.QUIT, self.quit),)),
            ("Ctrl+1", lambda: self.list.setFocus()),
            ("Ctrl+2", lambda: (ed := self.editor.current()) and ed.setFocus()),
            ("Ctrl+3", lambda: self.console.input.setFocus()),
        )
        for seq, slot in bindings:
            for key in keys.sequences(seq):
                QShortcut(key, self, slot)
        if keys.IS_MAC:
            self._build_mac_menus()

    def _build_mac_menus(self) -> None:
        """The global menu bar a Mac app is expected to have (it stays out of the window itself)."""
        bar = self.menuBar()
        assert bar is not None
        file_menu = bar.addMenu("File")
        assert file_menu is not None
        file_menu.addAction(self.act_new)
        file_menu.addSeparator()
        file_menu.addAction(self.act_start_all)
        file_menu.addAction(self.act_stop_all)
        file_menu.addAction(self.act_settings)  # moved into the application menu by its role
        self.act_settings.setShortcut(QKeySequence.StandardKey.Preferences)
        window_menu = bar.addMenu("Window")
        assert window_menu is not None
        minimize = window_menu.addAction("Minimize", QKeySequence("Ctrl+M"), self.showMinimized)
        assert minimize is not None
        window_menu.addAction("Show Pythonator", self.show_window)

    # -- selection ------------------------------------------------------------------------

    def current(self) -> Service | None:
        return self.sup.get(self._current) if self._current else None

    def _with_current(self, fn: object) -> None:
        if self._current and callable(fn):
            fn(self._current)

    def select(self, service_id: str | None) -> None:
        row = self.model.row_of(service_id) if service_id else -1
        if row >= 0 and (sel := self.list.selectionModel()):
            sel.setCurrentIndex(self.model.index(row), QItemSelectionModel.SelectionFlag.ClearAndSelect)
        else:
            self._set_current(None)

    def _on_list_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        svc = current.data(ServiceRole) if current.isValid() else None
        self._set_current(svc.id if isinstance(svc, Service) else None)

    def _set_current(self, service_id: str | None) -> None:
        if service_id == self._current and service_id is not None:
            return
        self._current = service_id
        svc = self.current()
        self.console.title.setText(f"Console — {svc.name}" if svc else "Console")
        if svc is None:
            self.console.show_lines([])
            self.console.set_question(None)
        else:
            self.console.show_lines(svc.log.snapshot())
            self.console.set_running(svc.status in (Status.RUNNING, Status.STOPPING))
            self.console.set_question(svc.question.question if svc.question else None)
            self._open_service_script(svc, focus=False)
        self._update_files()
        self._update_header()

    def _update_files(self) -> None:
        svc = self.current()
        if svc is None:
            self.files.show_message("Select a service to see its folder.")
            self.files.set_root(None)
            return
        cfg = svc.config
        if not cfg.workdir.strip() and cfg.mode is not LaunchMode.SCRIPT:
            self.files.show_message("Set a working folder in Configure to browse its files here.")
            self.files.set_root(None)
            return
        folder = resolve_workdir(cfg)
        self.files.show_message(f"{folder} doesn't exist yet.")
        self.files.set_root(folder)

    def _open_service_script(self, svc: Service, *, focus: bool) -> None:
        cfg = svc.config
        if cfg.mode is not LaunchMode.SCRIPT or not cfg.target:
            return
        path = Path(cfg.target).expanduser()
        if not path.is_absolute():
            path = resolve_workdir(cfg) / path
        if not path.is_file():
            return
        if cfg.runtime is Runtime.PYTHON:
            try:
                python, _ = resolve_python(cfg, resolve_workdir(cfg))
                self.editor.set_python_for(str(path), str(python))
            except LaunchError:
                pass
        self.editor.open_file(str(path), focus=focus)

    # -- service actions ------------------------------------------------------------------

    # Like an IDE: edited files are saved before (re)starting, so the service runs what you see.
    def start_service(self, service_id: str) -> None:
        self.editor.save_all()
        self.sup.start(service_id)

    def restart_service(self, service_id: str) -> None:
        self.editor.save_all()
        self.sup.restart(service_id)

    def start_or_restart(self, service_id: str) -> None:
        svc = self.sup.get(service_id)
        if svc and svc.status in (Status.RUNNING, Status.STOPPING):
            self.restart_service(service_id)
        else:
            self.start_service(service_id)

    def new_service(self, target: str = "") -> None:
        cfg = ServiceConfig(name=self.sup.unique_name("New service"))
        if target:
            cfg.target = target
            cfg.runtime = runtime_for_path(target)
            cfg.name = self.sup.unique_name(Path(target).stem)
            if (Path(target).parent / ".env").is_file():
                cfg.env_file = ".env"
        dlg = ServiceDialog(cfg, {s.name for s in self.sup.services()}, self, is_new=True)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            svc = self.sup.add(dlg.result_config())
            self.select(svc.id)
            self._open_service_script(svc, focus=True)

    def edit_service(self) -> None:
        svc = self.current()
        if svc is None:
            return
        dlg = ServiceDialog(svc.config, {s.name for s in self.sup.services()}, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.sup.update(dlg.result_config())
            if svc.status in (Status.RUNNING, Status.STOPPING):
                self.statusBar().showMessage("Saved. Restart the service to apply the changes.", 6000)  # type: ignore[union-attr]
            self._open_service_script(svc, focus=False)
            self._update_files()

    def duplicate_service(self, svc: Service) -> None:
        copy = svc.config.copy(id=new_id(), name=self.sup.unique_name(f"{svc.name} copy"), autostart=False,
                               env=dict(svc.config.env))
        self.select(self.sup.add(copy).id)

    def delete_service(self, svc: Service) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete service")
        running = " It is running and will be killed." if svc.status.active else ""
        box.setText(f"Delete “{svc.name}”?{running}")
        box.setInformativeText("Your script and its files are not touched.")
        delete_logs = QCheckBox("Also delete its log files")
        box.setCheckBox(delete_logs)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        row = self.model.row_of(svc.id)
        self.sup.remove(svc.id, delete_logs=delete_logs.isChecked())
        self.select(self.model.id_at(max(0, row - 1)))

    def _fill_service_menu(self, menu: QMenu, svc: Service | None) -> None:
        menu.clear()
        if svc is None:
            act = menu.addAction("New service…")
            assert act is not None
            act.triggered.connect(lambda: self.new_service())
            return
        running = svc.status in (Status.RUNNING, Status.STOPPING)

        def add(text: str, slot: object, enabled: bool = True) -> None:
            action = menu.addAction(text)
            assert action is not None
            action.setEnabled(enabled)
            if callable(slot):
                action.triggered.connect(lambda _c=False: slot())

        add("Start", lambda: self.start_service(svc.id), not running)
        add("Stop", lambda: self.sup.stop(svc.id), svc.status.active or svc.task is not None)
        add("Restart", lambda: self.restart_service(svc.id))
        menu.addSeparator()
        self._add_deps_actions(menu, svc)
        menu.addSeparator()
        if svc.log.url:
            add(f"Open {svc.log.url} in browser", self._open_service_url)
        add("Open script in editor", lambda: self._open_service_script(svc, focus=True),
            svc.config.mode is LaunchMode.SCRIPT)
        add("Open working folder", lambda: self._open_path(resolve_workdir(svc.config)))
        add("Open log file", lambda: self._open_path(svc.log.path))
        add("Search log history…", self._open_history)
        menu.addSeparator()
        add("Configure…", self.edit_service)
        add("Duplicate", lambda: self.duplicate_service(svc))
        add("Move up", lambda: self.sup.move(svc.id, -1))
        add("Move down", lambda: self.sup.move(svc.id, +1))
        menu.addSeparator()
        add("Delete…", lambda: self.delete_service(svc))

    def _fill_deps_menu(self) -> None:
        self.deps_menu.clear()
        if (svc := self.current()) is not None:
            self._add_deps_actions(self.deps_menu, svc)

    def _add_deps_actions(self, menu: QMenu, svc: Service) -> None:
        def add(text: str, slot: Callable[[], object], enabled: bool = True) -> None:
            action = menu.addAction(text)
            assert action is not None
            action.setEnabled(enabled)
            action.triggered.connect(lambda _c=False: slot())

        info = runtime_info(svc.config.runtime)
        workdir = resolve_workdir(svc.config)
        manifest = info.manifest(workdir)
        if svc.config.runtime is Runtime.PYTHON:
            add("Create venv", lambda: self.sup.create_venv(svc.id), svc.task is None)
        install = "Save and install dependencies" if self._manifest_modified(manifest) else "Install dependencies"
        add(install, lambda: self.install_dependencies(svc), svc.task is None and not info.no_install)
        if manifest is not None:
            add(f"Edit {manifest.name}", lambda: self.editor.open_file(str(manifest)))
        elif (starter := starter_for(svc.config, workdir)) is not None:
            add(f"Create {starter.name}", lambda: self._create_manifest(workdir, starter))
        if svc.config.requirements.strip():
            add("Edit package list (service settings)…", self.edit_service)
        add(f"Cancel “{svc.task_label}”" if svc.task else "Cancel task", lambda: self.sup.stop_task(svc.id),
            svc.task is not None)

    def _manifest_modified(self, manifest: Path | None) -> bool:
        if manifest is None:
            return False
        tab = self.editor.find_tab(str(manifest))
        return tab is not None and tab.is_modified()

    def install_dependencies(self, svc: Service) -> None:
        """Install, saving the open dependency file first so edits made in the editor are used."""
        workdir = resolve_workdir(svc.config)
        manifest = runtime_info(svc.config.runtime).manifest(workdir)
        if not svc.config.install_confirmed:
            what = ("listed in the service settings" if svc.config.requirements.strip()
                    else f"from {manifest.name}" if manifest else "this project needs")
            go_on, never_again = confirm_install(self, svc.name, what, str(workdir))
            if not go_on:
                return
            if never_again:
                self.sup.update(svc.config.copy(install_confirmed=True))
        if manifest is not None and not self.editor.save_all({str(manifest)}):
            self.statusBar().showMessage(f"Could not save {manifest.name}; not installing.", 6000)  # type: ignore[union-attr]
            return
        self.sup.install_requirements(svc.id)

    def _create_manifest(self, workdir: Path, starter: Starter) -> None:
        path = workdir / starter.name
        try:
            if not path.exists():
                path.write_text(starter.content, encoding="utf-8")
        except OSError as exc:
            self.statusBar().showMessage(f"Could not create {path}: {exc.strerror or exc}", 6000)  # type: ignore[union-attr]
            return
        self.editor.open_file(str(path))
        self.statusBar().showMessage(  # type: ignore[union-attr]
            f"Created {starter.name}. Add your packages, then 📦 Dependencies → Install dependencies.", 8000)

    def _list_menu(self, pos: QPoint) -> None:
        index = self.list.indexAt(pos)
        svc = index.data(ServiceRole) if index.isValid() else None
        if isinstance(svc, Service):
            self.select(svc.id)
        menu = QMenu(self)
        self._fill_service_menu(menu, svc if isinstance(svc, Service) else None)
        if viewport := self.list.viewport():
            menu.exec(viewport.mapToGlobal(pos))

    # -- updates --------------------------------------------------------------------------

    def _on_services_changed(self) -> None:
        current = self._current
        self.model.reload()
        self.empty_hint.setVisible(self.model.rowCount() == 0)
        if current and self.model.row_of(current) >= 0:
            self.select(current)
            svc = self.current()
            if svc:
                self.console.title.setText(f"Console — {svc.name}")
        self._update_header()

    def _on_service_changed(self, service_id: str) -> None:
        self.model.refresh(service_id)
        if service_id == self._current:
            self._flush()
            svc = self.current()
            if svc:
                self.console.set_running(svc.status in (Status.RUNNING, Status.STOPPING))
                self.console.set_question(svc.question.question if svc.question else None)
            self._update_header()
        self._update_counts()

    def _open_service_url(self) -> None:
        if (svc := self.current()) and svc.log.url:
            open_web_url(svc.log.url)

    def _flush(self) -> None:
        if (svc := self.current()) is not None:
            self.console.append(svc.log.drain())

    def _tick(self) -> None:
        self.model.refresh()
        self._update_header()
        self._update_counts()

    def _update_counts(self) -> None:
        total = len(self.sup.services())
        running = self.sup.running_count()
        self.sb_running.setText(f"  {running}/{total} running  ")
        self.act_stop_all.setEnabled(any(s.status.active for s in self.sup.services()))
        self.act_start_all.setEnabled(any(not s.status.active for s in self.sup.services()))
        if self.tray:
            self.tray.setToolTip(f"Pythonator — {running}/{total} running")
        self.empty_hint.setVisible(total == 0)

    def _update_header(self) -> None:
        svc = self.current()
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_deps, self.btn_edit, self.btn_more):
            b.setEnabled(svc is not None)
        url = svc.log.url if svc is not None and svc.status in (Status.RUNNING, Status.STOPPING) else None
        self.btn_web.setVisible(url is not None)
        self.btn_web.setToolTip(f"Open {url} in your browser" if url else "")
        if svc is None:
            self.h_name.setText("No service selected")
            self.h_info.setText("Create a service to run a script, web server or bot.")
            return
        st = svc.status
        color = theme.STATUS_COLORS[st]
        self.h_name.setText(
            f"{svc.name} <span style='font-size:11px; color:{color}'>● {theme.STATUS_LABELS[st]}</span>"
        )
        parts = [describe_target(svc)]
        if st in (Status.RUNNING, Status.STOPPING):
            parts.append(f"pid {svc.pid}")
            parts.append(f"up {format_uptime(svc.uptime)}")
            if stats := self.sup.stats_for(svc):
                parts.append(f"CPU {stats.cpu:.1f}%")
                parts.append(f"RAM {stats.rss_mb:.1f} MB")
                if stats.procs > 1:
                    parts.append(f"{stats.procs} processes")
        elif svc.last_exit is not None:
            parts.append(svc.last_exit.describe())
        if svc.restarts:
            parts.append(f"{svc.restarts} restarts")
        if svc.task:
            parts.append(f"⚙ {svc.task_label}…")
        if svc.next_run:
            parts.append(f"⏱ next run {format_next_run(svc.next_run)}")
        parts.append(f"restart: {svc.config.restart.value}")
        self.h_info.setText("   ·   ".join(parts))
        running = st in (Status.RUNNING, Status.STOPPING)
        self.btn_start.setEnabled(not running)
        task = svc.task
        self.btn_stop.setEnabled(st.active or task is not None)
        killing = st is Status.STOPPING or (task is not None and task.stop_requested)
        self.btn_stop.setText("✖  Kill" if killing else "■  Stop")
        self.btn_stop.setToolTip(f"Stop {svc.name}" + (f" and “{svc.task_label}”" if task else "")
                                 + f"; press again to kill ({keys.text(keys.STOP)})")

    # -- console --------------------------------------------------------------------------

    def _on_console_submit(self, mode: str, text: str) -> None:
        svc = self.current()
        if svc is None:
            self.statusBar().showMessage("Select a service first.", 4000)  # type: ignore[union-attr]
            return
        if self.sup.answer(svc.id, text):  # a y/n question (e.g. use another package version) is open
            self._flush()
            return
        if mode == "stdin":
            if not self.sup.send_input(svc.id, text):
                self.statusBar().showMessage(  # type: ignore[union-attr]
                    f"{svc.name} is not running. Use $ shell mode to run a command in its environment.", 6000
                )
        elif command := text.strip():
            if (reason := risky_command(command)) and not confirm_risky_command(self, command, reason):
                self.console.input.setText(command)  # keep it, so a typo can be fixed
                return
            self.sup.run_shell(svc.id, command)
        self._flush()

    def _clear_console(self) -> None:
        if svc := self.current():
            svc.log.clear()
        self.console.show_lines([])

    def _open_history(self) -> None:
        if svc := self.current():
            HistoryDialog(svc.name, svc.log, self).show()

    def _open_log(self) -> None:
        if svc := self.current():
            if not svc.log.path.exists():
                self.statusBar().showMessage("No log file yet.", 4000)  # type: ignore[union-attr]
                return
            self._open_path(svc.log.path)

    def _toggle_timestamps(self, on: bool) -> None:
        self.sup.config.settings.show_timestamps = on
        self.console.view.set_show_timestamps(on)
        if svc := self.current():
            self.console.show_lines(svc.log.snapshot())
        self.sup.save()

    def _toggle_wrap(self, on: bool) -> None:
        self.sup.config.settings.wrap_lines = on
        self.sup.save()

    def _apply_settings_to_console(self) -> None:
        st = self.sup.config.settings
        self.console.view.set_max_lines(st.console_lines)
        self.console.view.set_show_timestamps(st.show_timestamps)
        self.console.btn_time.setChecked(st.show_timestamps)
        self.console.btn_wrap.setChecked(st.wrap_lines)
        self.console.view.set_wrap(st.wrap_lines)
        try:
            self.console.set_shell_name(resolve_shell(st.console_shell).name)
        except LaunchError:
            self.console.set_shell_name("")

    def _open_location(self, path: str, line: int) -> None:
        if len(path) > 3 and path[0] == "/" and path[2] == ":":  # file:///C:/x.js
            path = path[1:]
        candidate = Path(path)
        if not candidate.is_absolute() and (svc := self.current()):
            candidate = resolve_workdir(svc.config) / candidate
        if candidate.is_file():
            self.editor.open_file(str(candidate), line)
        else:
            self.statusBar().showMessage(f"Not found: {candidate}", 4000)  # type: ignore[union-attr]

    @staticmethod
    def _open_path(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # -- settings, tray, drag & drop ------------------------------------------------------

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.sup, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        st = self.sup.config.settings
        st.console_lines = dlg.lines.value()
        st.log_max_mb = dlg.log_size.value()
        st.log_backups = dlg.backups.value()
        st.close_action = CloseAction(dlg.close_action.currentData())
        st.console_shell = ConsoleShell(dlg.console_shell.currentData())
        wanted = dlg.start_at_login.isChecked()
        if dlg.login_item.supported and wanted != dlg.login_item.is_enabled():
            try:
                dlg.login_item.set_enabled(wanted)
            except OSError as exc:
                QMessageBox.warning(self, "Start at login",
                                    f"Could not change {dlg.login_item.where()}:\n{exc}")
        self.sup.apply_settings()
        self._apply_settings_to_console()
        if svc := self.current():
            self.console.show_lines(svc.log.snapshot())

    def _on_notify(self, title: str, message: str) -> None:
        self.statusBar().showMessage(f"{title}: {message}", 10000)  # type: ignore[union-attr]
        if self.tray and (not self.isVisible() or not self.isActiveWindow()):
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Warning, 5000)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if keys.IS_MAC:
            return  # a click on a menu bar icon opens its menu, which has "Show Pythonator"
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        if e is not None and (mime := e.mimeData()) and mime.hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent | None) -> None:
        if e is None or (mime := e.mimeData()) is None:
            return
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        scripts = [p for p in paths if p.lower().endswith(SCRIPT_EXTENSIONS)]
        if len(scripts) == 1:
            self.new_service(scripts[0])
        else:
            for p in scripts:
                svc = self.sup.add(ServiceConfig(name=self.sup.unique_name(Path(p).stem), target=p,
                                                 runtime=runtime_for_path(p)))
                self.select(svc.id)
        for p in paths:
            if p not in scripts and Path(p).is_file():
                self.editor.open_file(p)

    # -- persistence & shutdown -----------------------------------------------------------

    def _restore_state(self) -> None:
        st = self.sup.config.settings
        if st.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(st.window_geometry.encode()))
        if st.splitter_state:
            states = st.splitter_state.split("|")
            for split, state in zip((self.main_split, self.work_split, self.side_split), states, strict=False):
                if state:
                    split.restoreState(QByteArray.fromBase64(state.encode()))

    def _save_state(self) -> None:
        st = self.sup.config.settings
        st.window_geometry = bytes(self.saveGeometry().toBase64().data()).decode()
        st.splitter_state = "|".join(
            bytes(split.saveState().toBase64().data()).decode()
            for split in (self.main_split, self.work_split, self.side_split)
        )
        self.sup.save()

    def quit(self, *, from_system: bool = False) -> None:
        """Stop the services and quit (tray menu, Ctrl+Q, Ctrl+C, or ``from_system``: ⌘Q, logout)."""
        self._quitting = True
        self._system_quit = from_system
        self.close()

    def closeEvent(self, e: QCloseEvent | None) -> None:
        if e is None:
            return
        active = [s for s in self.sup.services() if s.status.active]
        if not self._quitting and self.tray is not None:
            action: CloseAction | None = self.sup.config.settings.close_action
            if action is CloseAction.ASK and active:
                action = self._ask_close_action(len(active))
            if action is None:
                e.ignore()
                return
            if action is CloseAction.TRAY:
                e.ignore()
                self.hide()
                if not self._tray_hint_shown:
                    self._tray_hint_shown = True
                    how = "Click" if keys.IS_MAC else "Right-click"
                    self.tray.showMessage("Pythonator", f"Still running in the {TRAY}. {how} its icon to quit.",
                                          QSystemTrayIcon.MessageIcon.Information, 4000)
                return
        elif not self._quitting and active:
            answer = QMessageBox.question(
                self, "Services are running",
                f"Stop {len(active)} running service{'s' if len(active) != 1 else ''} and quit?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        if not self.editor.confirm_close_all():
            self._quitting = self._system_quit = False
            e.ignore()
            return
        try:
            self._save_state()
            if active:
                self.statusBar().showMessage("Stopping services…")  # type: ignore[union-attr]
                QApplication.processEvents()
            self._flush_timer.stop()
            self._stats_timer.stop()
            self.sup.shutdown()
        finally:  # whatever happens while stopping, the app must still be able to quit
            if self.tray:
                self.tray.hide()
            e.accept()
            self.closed = True
            if not self._system_quit:  # a platform quit request carries on by itself once accepted
                QApplication.quit()

    def _ask_close_action(self, n_active: int) -> CloseAction | None:
        box = QMessageBox(self)
        box.setWindowTitle("Services are running")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"{n_active} service{'s are' if n_active != 1 else ' is'} still running.")
        box.setInformativeText(f"Keep them running in the {TRAY}, or stop them and quit?")
        tray_btn = box.addButton(f"Keep running in the {TRAY}", QMessageBox.ButtonRole.AcceptRole)
        quit_btn = box.addButton("Stop all and quit", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        remember = QCheckBox("Remember my choice")
        box.setCheckBox(remember)
        box.setDefaultButton(tray_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is tray_btn:
            choice = CloseAction.TRAY
        elif clicked is quit_btn:
            choice = CloseAction.QUIT
        else:
            return None
        if remember.isChecked():
            self.sup.config.settings.close_action = choice
            self.sup.save()
        return choice
