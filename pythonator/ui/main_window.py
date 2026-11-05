"""The main window: 0.0.9's layout on 0.0.10's engine. Every action is a request to the background service, so
nothing here waits: Start all and Stop all return at once and the list follows the service's state events."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QByteArray, QEvent, QObject, Qt, QTimer, QUrl
from PyQt6.QtGui import QAction, QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent, QKeySequence, QResizeEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import runtimes
from ..core.models import Mode, Service, Settings
from ..daemon.client import DaemonClient
from ..paths import IS_WINDOWS, DataDirs
from . import theme
from .about import SITE, AboutDialog, LicensingDialog, SupportDialog, SystemCheckDialog
from .console import Console
from .editor import EditorPanel
from .git_dialog import GitDialog
from .panels import PanelDock
from .plugins_view import PluginsDialog
from .proposal_dialog import ProposalDialog
from .service_dialog import ServiceDialog
from .service_list import MODE_LABEL, ServiceList, entry_text, human_bytes, status_text
from .system_meter import SystemMeter

HEADER_TITLE_ROOM = 380  # px the name and info line keep before the header buttons shrink to symbols
MODE_TIPS = {
    "sandbox": "Sandbox: it can change only its own folder; your other files, keys and Pythonator's data are out "
               "of reach.",
    "container": "Container: only its working folder is mounted; no root powers.",
    "full": "Full access: it runs with your full permissions and can read and change all your files.",
}


class Elided(QLabel):
    """One line that ends in "…" when it doesn't fit; the whole text is in the tooltip."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name)
        self.full = ""
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set(self, text: str) -> None:
        if text != self.full:
            self.full = text
            theme.set_tooltip(self, text)
            self._elide()

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideRight, max(0, self.width() - 2)))


def button(text: str, tip: str, *, prop: str = "") -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if prop:
        b.setProperty(prop, True)
    return b


def short(text: str) -> str:
    """ "▶ Start" -> "▶" for a narrow window; the tooltip keeps the words."""
    head, _, rest = text.partition(" ")
    return head if rest and len(head) == 1 and not head.isalnum() else {"Packages ▾": "Pkgs ▾"}.get(text, text)


class MainWindow(QMainWindow):
    def __init__(self, dirs: DataDirs, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Pythonator")
        self.resize(1360, 860)
        self.setAcceptDrops(True)
        self.dirs = dirs
        self.client = DaemonClient(dirs, self)
        self.services: dict[str, Service] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.settings = settings or Settings()  # the saved ones, until the background service sends its own
        self.plugins: list[dict[str, Any]] = []
        self.capabilities: list[tuple[str, str]] = []
        self.current = ""
        self.plugins_dialog: PluginsDialog | None = None
        self.proposals: dict[str, ProposalDialog] = {}
        self._restored = False
        self._quitting = False  # closing for real, not into the tray
        self.tray: QSystemTrayIcon | None = None
        self._icons: tuple[Any, ...] = ()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self.client.message.connect(self._on_message)
        self.client.connected.connect(self._on_connected)
        self.client.disconnected.connect(lambda: self._status("Background service lost; reconnecting…"))
        self._status("Connecting to the background service…")
        self._set_services([])
        self._update_tray()
        QTimer.singleShot(0, self.client.connect_daemon)  # after the first paint: the window never waits for it

    # -- construction -------------------------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        self.addToolBar(tb)
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)

        def act(text: str, tip: str, slot: Callable[[], object], keys: str = "") -> QAction:
            a = QAction(text, self)
            a.setToolTip(tip + (f" ({keys})" if keys else ""))
            if keys:
                a.setShortcut(QKeySequence(keys))
            a.triggered.connect(lambda _c=False: slot())
            tb.addAction(a)
            return a

        act("+ New service", "Add a script, server or bot", self.new_service, "Ctrl+N")
        act("⎇ From Git", "Clone a repository (https:// or SSH) and run it", self.from_git)
        tb.addSeparator()
        self.act_start_all = act("▶ Start all", "Start every service", lambda: self.client.request({"op": "start_all"}))
        self.act_stop_all = act("■ Stop all", "Stop every service; press again to kill what is still stopping",
                                lambda: self.client.request({"op": "stop_all"}))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.meter = tb.addWidget(SystemMeter())
        tb.addSeparator()
        self.plugins_button = button("Plugins ▾", "Turn plugins on and off, set them up, show their panels")
        self.plugins_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(self.plugins_button)
        act("System check", "What Pythonator runs on and what the sandbox enforces", self.system_check)
        act("Settings", "Starting services, logs, the tray and starting with the computer", self.show_settings)
        help_button = button("Help ▾", "Documentation, licensing, support and credits")
        help_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        menu.addAction("Documentation", lambda: QDesktopServices.openUrl(QUrl(f"{SITE}/docs/")))
        menu.addSeparator()
        menu.addAction("Licensing", lambda: LicensingDialog(self).exec())
        menu.addAction("Support", lambda: SupportDialog(self).exec())
        menu.addSeparator()
        about = menu.addAction("About Pythonator", lambda: AboutDialog(self).exec())
        if about is not None:
            about.setMenuRole(QAction.MenuRole.AboutRole)  # macOS: in the application menu
        help_button.setMenu(menu)
        tb.addWidget(help_button)

    def _build_body(self) -> None:
        pane = QWidget()
        pane.setObjectName("SidebarPane")
        sl = QVBoxLayout(pane)
        sl.setContentsMargins(0, 10, 0, 0)
        sl.setSpacing(6)
        title = QLabel("SERVICES")
        title.setProperty("section", True)
        title.setContentsMargins(14, 0, 0, 0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 8, 0)
        head.addWidget(title, 1)
        self.list = ServiceList()
        for text, tip, step in (("↑", "Move the selected services up (Alt+↑)", -1),
                                ("↓", "Move the selected services down (Alt+↓)", 1)):
            b = button(text, tip + ": Start all and sign-in start them in this order")
            b.setAutoRaise(True)
            b.clicked.connect(lambda _c=False, st=step: self.list.shift(st))
            head.addWidget(b)
        sl.addLayout(head)
        self.list.selected.connect(self._select)
        self.list.reordered.connect(lambda ids: self.client.request({"op": "reorder", "ids": ids}))
        self.list.remove_requested.connect(self.remove_service)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(
            lambda pos: self._service_menu(QMenu(self)).exec(self.list.mapToGlobal(pos)) if self.current else None)
        self.list.doubleClicked.connect(lambda _i: self.edit_service())
        sl.addWidget(self.list, 1)
        self.empty_hint = QLabel("No services yet.\n\nClick “New service” or drop a script\n"
                                 "(.py, .js, .go, .jar, .rb, .php, .sh …)\nonto the window.")
        self.empty_hint.setProperty("muted", True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self.empty_hint, 1)

        self.editor = EditorPanel(self.settings.editor_font_size)
        self.editor.status.connect(lambda text: self._status(text, 5000))
        side = QSplitter(Qt.Orientation.Vertical)
        side.setObjectName("Sidebar")
        side.addWidget(pane)
        side.addWidget(self.editor.files)
        side.setStretchFactor(0, 2)
        side.setStretchFactor(1, 3)
        side.setSizes([330, 470])
        side.setMinimumWidth(230)

        header = QWidget()
        header.setObjectName("Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 8, 12, 8)
        hl.setSpacing(6)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.h_name = Elided("HeaderTitle")
        self.h_name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.h_state = QLabel()
        self.h_state.setFixedSize(20, 20)
        self.h_mode = QLabel()
        self.h_mode.setFixedSize(22, 20)
        self.h_mode.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_row.addWidget(self.h_name, 1)
        name_row.addWidget(self.h_state)
        name_row.addWidget(self.h_mode)
        titles.addLayout(name_row)
        self.h_info = Elided("HeaderInfo")
        titles.addWidget(self.h_info)
        hl.addLayout(titles, 1)
        hl.addSpacing(12)
        self.b_open = button("↗ Open", "Open this server in your browser")
        self.b_start = button("▶ Start", "Start (F5)", prop="accent")
        self.b_stop = button("■ Stop", "Stop gracefully; press again to kill (Shift+F5)")
        self.b_restart = button("↻ Restart", "Restart (Ctrl+R)")
        self.b_packages = button("Packages ▾", "Install packages, recreate the environment, or pull from Git")
        self.b_configure = button("⚙ Configure", "Edit this service (Ctrl+E)")
        self.b_more = button("⋯", "More actions")
        self.b_packages.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        packages = QMenu(self)
        self.a_install = packages.addAction("Install packages", lambda: self._op("install"))
        self.a_recreate = packages.addAction("Recreate environment…", self._recreate)
        self.a_pull = packages.addAction("Pull from Git (fast-forward only)", lambda: self._op("pull"))
        self.b_packages.setMenu(packages)
        self.b_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self)
        more.aboutToShow.connect(lambda: self._service_menu(more))
        self.b_more.setMenu(more)
        self.b_open.clicked.connect(self.open_in_browser)
        self.b_start.clicked.connect(lambda: self._op("start"))
        self.b_stop.clicked.connect(lambda: self._op("stop"))
        self.b_restart.clicked.connect(lambda: self._op("restart"))
        self.b_configure.clicked.connect(self.edit_service)
        self.buttons = (self.b_open, self.b_start, self.b_stop, self.b_restart, self.b_packages, self.b_configure,
                        self.b_more)
        self.labels = {b: b.text() for b in self.buttons}
        self.compact = False
        for b in self.buttons:
            hl.addWidget(b)
        self.header = header
        header.installEventFilter(self)

        self.console = Console(self.settings.console_lines)
        self.console.submitted.connect(self._send_input)
        self.console.shell_submitted.connect(self._send_shell)
        self.console.clear_requested.connect(self.clear_console)
        self.console.open_log_requested.connect(self.open_log)
        work = QSplitter(Qt.Orientation.Vertical)
        work.addWidget(self.editor)
        work.addWidget(self.console)
        work.setStretchFactor(0, 5)
        work.setStretchFactor(1, 4)
        work.setSizes([480, 360])
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(header)
        rl.addWidget(work, 1)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(side)
        main.addWidget(right)
        main.setStretchFactor(1, 1)
        main.setSizes([270, 1090])
        main.setCollapsible(1, False)
        self.setCentralWidget(main)

        self.panels = PanelDock(lambda plugin, action, value: self.client.request(
            {"op": "plugin_action", "plugin": plugin, "action": action, "value": value}))
        self.dock = QDockWidget("Plugins", self)
        self.dock.setObjectName("plugin-panels")
        self.dock.setWidget(self.panels)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                              | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()  # out of the way until asked for: the editor and console keep the width
        plugins = QMenu(self)
        plugins.addAction("Manage plugins…", self.show_plugins)
        panels = self.dock.toggleViewAction()
        if panels is not None:
            panels.setText("Show plugin panels")
            plugins.addAction(panels)
            plugins.aboutToShow.connect(lambda: panels.setEnabled(self.panels.count() > 0))
        self.plugins_button.setMenu(plugins)

        for keys, slot in (("F5", lambda: self._op("restart" if self._live() else "start")),
                           ("Shift+F5", lambda: self._op("stop")), ("Ctrl+R", lambda: self._op("restart")),
                           ("Ctrl+E", self.edit_service)):
            a = QAction(self)
            a.setShortcut(QKeySequence(keys))
            a.triggered.connect(lambda _c=False, f=slot: f())
            self.addAction(a)

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.sb_cursor = QLabel("")
        self.sb_running = QLabel("")
        bar.addPermanentWidget(self.sb_cursor)
        bar.addPermanentWidget(self.sb_running)
        self.editor.cursor_info.connect(self.sb_cursor.setText)

    def _status(self, text: str, ms: int = 0) -> None:
        if bar := self.statusBar():
            bar.showMessage(text, ms)

    # -- the background service ---------------------------------------------------------------------------

    def _on_connected(self) -> None:
        self._status("Connected", 3000)
        self.client.request({"op": "watch", "id": self.current})  # only the shown service's output comes here
        self.client.request({"op": "capabilities"}, self._got_capabilities)

    def _got_capabilities(self, ok: bool, data: Any, _error: str) -> None:
        if ok and isinstance(data, dict):
            self.capabilities = [(str(k), str(v)) for k, v in data.get("sandbox", [])]

    def _on_message(self, msg: dict[str, Any]) -> None:
        ev = msg.get("ev")
        if ev == "snapshot":
            self.settings = Settings.model_validate(msg.get("settings", {}))
            self._set_services(msg["services"])
            for st in msg["states"]:
                self._on_state(st)
            self._update_tray()
            if not self._restored:
                self._restored = True
                self.editor.set_font_size(self.settings.editor_font_size)
                if self.settings.window_geometry:
                    self.restoreGeometry(QByteArray.fromHex(self.settings.window_geometry.encode()))
        elif ev == "services":
            self._set_services(msg["services"])
        elif ev == "state":
            self._on_state(msg)
        elif ev == "output":
            if msg.get("id") == self.current:
                self.console.append(str(msg.get("text", "")))
        elif ev == "stats":
            items = msg.get("items", {})
            self.stats = dict(items)
            self.list.set_stats(items)
            self._update_header()
        elif ev == "notice":
            self._notice(str(msg.get("level", "info")), str(msg.get("text", "")))
        elif ev == "plugins":
            self.plugins = list(msg.get("plugins", []))
            if self.plugins_dialog is not None:
                self.plugins_dialog.refresh(self.plugins)
        elif ev == "plugin_activity" and self.plugins_dialog is not None:
            self.plugins_dialog.append_activity(str(msg.get("plugin")), str(msg.get("text", "")))
        elif ev == "proposal":
            self._show_proposal(msg.get("proposal"))
        elif ev == "proposal_done":
            dialog = self.proposals.pop(str(msg.get("id", "")), None)
            if dialog is not None:
                dialog.closed_elsewhere()
        elif ev == "panel":
            self.panels.update_panel(str(msg.get("plugin")), str(msg.get("name", "")), msg.get("panel"))
            if not self.panels.count():
                self.dock.hide()

    def _set_services(self, items: list[dict[str, Any]]) -> None:
        services: dict[str, Service] = {}
        for data in items:
            try:
                s = Service.model_validate(data)
            except ValueError:
                continue
            services[s.id] = s
        self.services = services
        self.states = {k: v for k, v in self.states.items() if k in services}
        self.list.set_services(list(services.values()))
        self.empty_hint.setVisible(not services)
        self.list.setVisible(bool(services))
        self._update_header()
        self._update_counts()

    def _on_state(self, st: dict[str, Any]) -> None:
        sid = str(st.get("id"))
        self.states[sid] = st
        self.list.set_state(st)
        if sid == self.current:
            self._update_header()
        self._update_counts()

    def _show_proposal(self, proposal: Any) -> None:
        """A plugin suggests a change: the review window, never modal, never applied without the user."""
        if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
            return
        pid = proposal["id"]
        if pid in self.proposals:
            return
        dialog = ProposalDialog(proposal, self.client.request, self)
        dialog.finished.connect(lambda *_: self.proposals.pop(pid, None))
        self.proposals[pid] = dialog
        dialog.show()

    def _notice(self, level: str, text: str) -> None:
        self._status(text, 8000)
        if level == "error":
            QMessageBox.warning(self, "Pythonator", text)

    # -- the selected service -----------------------------------------------------------------------------

    def _state(self) -> str:
        return str(self.states.get(self.current, {}).get("state", "stopped"))

    def _live(self) -> bool:
        return self._state() in theme.LIVE

    def _select(self, sid: str) -> None:
        if sid == self.current:
            return
        self.current = sid
        self.console.clear()
        service = self.services.get(sid)
        self.editor.set_root(service.folder if service else "", service.name if service else "")
        self.console.title.setText(f"Console · {service.name}" if service else "Console")
        self.client.request({"op": "watch", "id": sid if service else ""})
        if service:
            self.client.request({"op": "tail", "id": sid}, lambda ok, data, _e: self.console.set_text(str(data))
                                if ok and self.current == sid else None)
            entry = Path(service.folder, service.entry) if service.entry else None
            if entry is not None and entry.is_file():
                self.editor.open(entry)
        self._update_header()

    def web_url(self) -> str:
        service = self.services.get(self.current)
        if service is None or self._state() != "running":
            return ""
        ports = list(self.stats.get(service.id, {}).get("ports") or service.network.ports)
        return f"http://127.0.0.1:{ports[0]}/" if ports else ""

    def _update_header(self) -> None:
        service = self.services.get(self.current)
        for b in self.buttons:
            b.setEnabled(service is not None)
        url = self.web_url()
        self.b_open.setVisible(bool(url))
        if url:
            tip = f"Open {url} in your browser"
            if IS_WINDOWS and service is not None and service.mode.value == "sandbox":
                tip += "\nSandboxed on Windows: the browser reaches it after Configure → Make reachable…"
            theme.set_tooltip(self.b_open, tip)
        if service is None:
            self.h_name.set("No service selected" if self.services else "Add your first service")
            self.h_state.hide()
            self.h_mode.hide()
            self.h_info.set("" if self.services else "Create a service to run a script, web server or bot.")
            self._fit_header()
            return
        st = self.states.get(service.id, {})
        state = self._state()
        self.h_name.set(service.name)
        self._header_icons(service, state)
        rt = runtimes.RUNTIMES.get(service.runtime)
        row = self.list.row(service.id)
        parts = [status_text(row) if row else state.capitalize(),
                 f"{rt.label if rt else 'Command'} · {entry_text(service)}"]
        live = self.stats.get(service.id, {})
        if state in ("running", "stopping"):
            if st.get("pid"):
                parts.append(f"pid {st['pid']}")
            if live:
                parts.append(f"CPU {float(live.get('cpu', 0)):.1f}%"
                             + (f" / {service.cpu_percent}%" if service.cpu_percent else ""))
                parts.append(f"RAM {human_bytes(float(live.get('memory', 0)))}"
                             + (f" / {service.memory_mb} MB" if service.memory_mb else ""))
                if int(live.get("processes", 0)) > 1:
                    parts.append(f"{live['processes']} processes")
                if live.get("ports"):
                    parts.append("listening on " + ", ".join(map(str, live["ports"])))
        if st.get("restarts"):
            parts.append(f"{st['restarts']} restarts")
        if st.get("note"):
            parts.append(str(st["note"]))
        parts.append(f"restart: {service.restart.value}")
        self.h_info.set("  ·  ".join(parts))
        self.b_start.setEnabled(state not in theme.LIVE)
        self.b_stop.setEnabled(state in (*theme.LIVE, "waiting"))
        self._label(self.b_stop, "✖ Kill" if state == "stopping" else "■ Stop")
        if self.a_install is not None and self.a_pull is not None and self.a_recreate is not None:
            self.a_install.setEnabled(state not in theme.LIVE)
            self.a_recreate.setEnabled(state not in theme.LIVE)
            self.a_pull.setEnabled(state not in theme.LIVE and Path(service.folder, ".git").exists())
        self._fit_header()

    def _header_icons(self, service: Service, state: str) -> None:
        """Re-set the pixmaps and tooltips only when something changed: doing it every second makes tooltips
        flicker."""
        mode = service.mode.value
        offline = mode != "full" and not service.network.enabled
        key = (service.id, state, mode, offline, tuple(service.network.ports))
        if key == self._icons:
            return
        self._icons = key
        ratio = self.devicePixelRatioF()
        self.h_state.setPixmap(theme.state_pixmap(state, 20, ratio))
        theme.set_tooltip(self.h_state, theme.STATE_LABELS.get(state, state.capitalize()))
        self.h_mode.setPixmap(theme.mode_pixmap(mode, 14, ratio, offline))
        c = theme.mode_color(mode)
        theme.set_style(self.h_mode, f"QLabel {{ background: {theme.rgba(c, 0.12)}; border: 1px solid "
                                     f"{theme.rgba(c, 0.32)}; border-radius: {theme.R_SM}px; }}")
        net = "" if mode == "full" else "\nNetwork: off" if offline else "\nNetwork: on"
        if service.network.ports:
            net += " · serves on " + ", ".join(map(str, service.network.ports))
        theme.set_tooltip(self.h_mode, f"{MODE_LABEL.get(mode, mode)}. {MODE_TIPS.get(mode, '')}{net}")
        self.h_state.show()
        self.h_mode.show()

    def _update_counts(self) -> None:
        running = sum(1 for st in self.states.values() if st.get("state") in theme.LIVE)
        self.sb_running.setText(f"{running} of {len(self.services)} running" if self.services else "")
        self.act_stop_all.setEnabled(running > 0 or any(
            st.get("state") == "waiting" for st in self.states.values()))

    # -- header fitting: full labels when they fit, symbols when the window is narrow ---------------------

    def _label(self, b: QToolButton, text: str) -> None:
        self.labels[b] = text
        b.setText(short(text) if self.compact else text)

    def _fit_header(self) -> None:
        fm = self.b_start.fontMetrics()
        full = sum(fm.horizontalAdvance(t) + 30 for b, t in self.labels.items() if b.isVisibleTo(self.header))
        compact = self.header.width() - HEADER_TITLE_ROOM < full
        if compact != self.compact:
            self.compact = compact
            for b, text in self.labels.items():
                b.setText(short(text) if compact else text)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        if self.meter is not None:
            self.meter.setVisible(self.width() >= 1100)  # a narrow window keeps the buttons, not the meter

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if obj is self.header and event is not None and event.type() == QEvent.Type.Resize:
            self._fit_header()  # also when the plugin panels open or a splitter moves
        return super().eventFilter(obj, event)

    # -- actions ------------------------------------------------------------------------------------------

    def _op(self, op: str, *, sid: str = "", confirmed: bool = False) -> None:
        sid = sid or self.current
        if not sid:
            return
        message: dict[str, Any] = {"op": op, "id": sid}
        if op == "start":
            message["confirmed"] = confirmed
        self.client.request(message, lambda ok, _d, err: self._op_done(op, sid, ok, err))

    def _op_each(self, op: str, ids: list[str]) -> None:
        for sid in ids:
            self._op(op, sid=sid)

    def _recreate(self) -> None:
        service = self.services.get(self.current)
        if service is None:
            return
        folder = Path(service.folder)
        container = service.mode is Mode.CONTAINER
        names = [p.relative_to(folder).as_posix() for p in runtimes.env_folders(service.runtime, folder,
                                                                                 container=container) if p.exists()]
        if not container and service.runtime in runtimes.CACHE_VARS:
            names.append(f"its private {runtimes.RUNTIMES[service.runtime].label} package cache")
        what = ", ".join(names) or "nothing yet"
        if QMessageBox.question(self, "Recreate environment", f"Delete {what} and install {service.name}'s packages "
                                "again from scratch?\n\nYour code and data aren't touched.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._op("recreate")

    def _op_done(self, op: str, sid: str, ok: bool, error: str) -> None:
        if ok:
            return
        if error == "confirm_full" and op == "start":
            name = self.services[sid].name if sid in self.services else "This service"
            if QMessageBox.warning(self, "Full access", f"{name} runs with Full access: it can read and change "
                                   "everything your account can. Start it anyway?\n\nSandbox mode keeps it to its "
                                   "own folder.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self._op("start", sid=sid, confirmed=True)
            return
        QMessageBox.warning(self, "Pythonator", error)

    def _send_input(self, text: str) -> None:
        if self.current:
            self.client.request({"op": "input", "id": self.current, "text": text},
                                lambda ok, _d, err: None if ok else self._status(err, 4000))

    def _send_shell(self, line: str) -> None:
        if self.current:
            self.client.request({"op": "shell", "id": self.current, "line": line},
                                lambda ok, _d, err: None if ok else self._status(err, 6000))

    def new_service(self, preset: dict[str, Any] | None = None) -> None:
        preset = dict(preset or {})
        preset.setdefault("mode", self.settings.default_mode.value)
        dialog = ServiceDialog(None, self.capabilities, self, preset=preset)
        if dialog.exec() and dialog.result_service is not None:
            sid = dialog.result_service.id
            self.client.request({"op": "add", "service": dialog.result_service.model_dump(mode="json")},
                                lambda ok, _d, err: QTimer.singleShot(200, lambda: self.list.select(sid))
                                if ok else self._notice("error", err))

    def edit_service(self) -> None:
        service = self.services.get(self.current)
        if service is None:
            return
        dialog = ServiceDialog(service, self.capabilities, self)
        if dialog.exec() and dialog.result_service is not None:
            self.client.request({"op": "update", "service": dialog.result_service.model_dump(mode="json")},
                                lambda ok, _d, err: None if ok else self._notice("error", err))

    def remove_service(self) -> None:
        """Remove the selected services (one or several), after one question."""
        chosen = [self.services[i] for i in self.list.selected_ids() if i in self.services]
        if not chosen:
            return
        names = ", ".join(s.name for s in chosen[:5]) + (f" and {len(chosen) - 5} more" if len(chosen) > 5 else "")
        what = names if len(chosen) == 1 else f"{len(chosen)} services ({names})"
        if QMessageBox.question(self, "Remove", f"Remove {what}? Their files stay where they are; their logs and "
                                "sandbox folders are deleted.") == QMessageBox.StandardButton.Yes:
            for service in chosen:
                self.client.request({"op": "remove", "id": service.id})

    def from_git(self) -> None:
        dialog = GitDialog(self)
        if dialog.exec() and dialog.folder is not None:
            rt, entry = runtimes.detect(dialog.folder)
            self.new_service({"name": dialog.folder.name[:64], "folder": str(dialog.folder),
                              "runtime": rt.key if rt else "python", "entry": entry,
                              "git": {"url": dialog.url.text(), "branch": dialog.branch.text(),
                                      "pull_on_start": False}})

    def show_plugins(self) -> None:
        if self.plugins_dialog is None:
            self.plugins_dialog = PluginsDialog(self.client.request, self)
        self.plugins_dialog.refresh(self.plugins)
        self.plugins_dialog.show()
        self.plugins_dialog.raise_()

    def show_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.settings, self.client.request, self)
        if dialog.exec():
            self.settings = dialog.settings
            self._update_tray()

    # -- the tray -----------------------------------------------------------------------------------------

    def _update_tray(self) -> None:
        """Settings → Window: closing the window leaves Pythonator in the tray."""
        want = self.settings.tray and QSystemTrayIcon.isSystemTrayAvailable()
        if app := QApplication.instance():
            app.setQuitOnLastWindowClosed(not want)  # type: ignore[attr-defined]
        if not want:
            if self.tray is not None:
                self.tray.hide()
                self.tray.deleteLater()
                self.tray = None
            return
        if self.tray is not None:
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Pythonator")
        menu = QMenu(self)
        menu.addAction("Show Pythonator", self.show_window)
        menu.addSeparator()
        menu.addAction("Start all", self.act_start_all.trigger)
        menu.addAction("Stop all", self.act_stop_all.trigger)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda why: self.show_window() if why == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def show_window(self) -> None:
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self._quitting = True
        if self.isHidden():
            self.show()  # the questions on closing need a window
        if self.close():
            QApplication.quit()
        else:
            self._quitting = False

    def system_check(self) -> None:
        def show(ok: bool, data: Any, error: str) -> None:
            if ok and isinstance(data, dict):
                SystemCheckDialog(data, self).exec()
            else:
                self._notice("error", error)

        self.client.request({"op": "capabilities"}, show)

    def open_in_browser(self) -> None:
        if url := self.web_url():
            QDesktopServices.openUrl(QUrl(url))

    def open_log(self) -> None:
        log = self.dirs.logs / f"{self.current}.log"
        if self.current and log.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))
        else:
            self._status("No log yet: it starts with the first run", 4000)

    def clear_console(self) -> None:
        if self.current:
            self.client.request({"op": "clear", "id": self.current})
            self.console.clear()

    def _copy(self, text: str) -> None:
        if clipboard := QApplication.clipboard():
            clipboard.setText(text)

    def _service_menu(self, menu: QMenu) -> QMenu:
        menu.clear()
        service = self.services.get(self.current)
        if service is None:
            return menu
        chosen = self.list.selected_ids()
        if len(chosen) > 1:
            n = len(chosen)
            menu.addAction(f"Start {n} services", lambda: self.client.request({"op": "start_all", "ids": chosen}))
            menu.addAction(f"Stop {n} services", lambda: self._op_each("stop", chosen))
        else:
            menu.addAction("Configure…", self.edit_service)
            menu.addAction("Open folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(service.folder)))
            if url := self.web_url():
                menu.addAction(f"Copy address ({url})", lambda: self._copy(url))
            menu.addAction("Open log", self.open_log)
            menu.addAction("Clear console", self.clear_console)
        menu.addSeparator()
        menu.addAction("Move up\tAlt+↑", lambda: self.list.shift(-1))
        menu.addAction("Move down\tAlt+↓", lambda: self.list.shift(1))
        menu.addSeparator()
        menu.addAction(f"Remove {len(chosen)} services…" if len(chosen) > 1 else "Remove…", self.remove_service)
        return menu

    # -- drag a script or folder onto the window ----------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        mime = event.mimeData() if event else None
        if event and mime and mime.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        mime = event.mimeData() if event else None
        if not mime or not mime.urls():
            return
        path = Path(mime.urls()[0].toLocalFile())
        if path.is_dir():
            rt, entry = runtimes.detect(path)
            folder = path
        else:
            rt, entry, folder = runtimes.for_entry(path.name), path.name, path.parent
        self.new_service({"name": (path.stem if path.is_file() else path.name)[:64], "folder": str(folder),
                          "runtime": rt.key if rt else "custom", "entry": entry})

    # -- closing ------------------------------------------------------------------------------------------

    def _remember_window(self) -> None:
        if self.client.ready:
            geometry = bytes(self.saveGeometry().toHex().data()).decode()
            self.settings = self.settings.model_copy(
                update={"window_geometry": geometry, "editor_font_size": self.editor.font_size})
            self.client.request({"op": "settings", "settings": self.settings.model_dump(mode="json")})

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if self.tray is not None and not self._quitting:  # into the tray; services keep running
            self._remember_window()
            self.hide()
            if event:
                event.ignore()
            return
        if self.editor.unsaved() and QMessageBox.question(
                self, "Unsaved files", "Some files have unsaved changes. Close anyway?") \
                != QMessageBox.StandardButton.Yes:
            if event:
                event.ignore()
            return
        self._remember_window()  # before anything else
        running = [s for s, st in self.states.items() if st.get("state") in (*theme.LIVE, "waiting")]
        if running and self.client.ready:
            box = QMessageBox(QMessageBox.Icon.Question, "Close Pythonator",
                              f"{len(running)} service(s) are running or scheduled. They can keep running in the "
                              "background; open Pythonator again to see them.", parent=self)
            keep = box.addButton("Keep them running", QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton("Stop everything and quit", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is stop:
                self.client.shutdown(stop_services=True)
            elif box.clickedButton() is not keep:
                if event:
                    event.ignore()
                return
        elif self.client.ready:
            self.client.shutdown(stop_services=True)  # nothing to look after: don't leave a process behind
        self.client.sock.waitForBytesWritten(500)
        self.client.close()
        if event:
            event.accept()
