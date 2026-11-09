"""The main window. It only shows and asks: every action is a request to pythonatord, which owns the services.
Layout: top bar (brand, search Ctrl K, New service, notifications, theme), a rail of pages, the page, a status bar.
Below 1180 px the inspector becomes a drawer; below 640 px one pane shows at a time with a bottom bar. Nothing
scrolls sideways. Pages other than Services are built the first time you open them."""

from __future__ import annotations

import sys
import time
from html import escape
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QElapsedTimer, QEvent, QObject, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QToolTip,
    QWidget,
)

from pythonator import __version__
from pythonator.store.files import data_dir
from pythonator.ui import theme as th
from pythonator.ui.icons import icon
from pythonator.ui.kit import Badge, Btn, Chip, Dot, Icon, Search, frame, hbox, label, primary, retheme, show_if, vbox
from pythonator.ui.link import Link
from pythonator.ui.service import ServicesPage
from pythonator.ui.widgets import dur

RES = Path(__file__).resolve().parent.parent / "resources"
RAIL = (
    ("services", "services", "Services"), ("files", "folder", "Files and editor"), ("engines", "box", "Engines"),
    ("plugins", "plug", "Plugins"), ("security", "shield-ok", "Security"),
)

class Main(QMainWindow):
    def __init__(self, t0: QElapsedTimer) -> None:
        super().__init__()
        self.t0 = t0
        self.painted = False
        self.root = data_dir()
        self.link = Link(self.root, self)
        self.services: list[dict[str, Any]] = []
        self.snap: dict[str, Any] = {}
        self.current = ""
        self.notes: list[dict[str, Any]] = []
        self.traffic: dict[str, list[dict[str, Any]]] = {}
        self.lazy: dict[str, QWidget] = {}
        self.plugins_snap: dict[str, Any] = {}
        self.show_work = False
        self.setWindowTitle("Pythonator")
        self.setWindowIcon(QIcon(str(RES / "icon-256.png")))
        scr = self.screen().availableGeometry() if self.screen() else None  # a bit smaller than the screen
        self.resize(min(1320, int(scr.width() * 0.82)) if scr else 1320, min(820, int(scr.height() * 0.82)) if scr else 820)
        self.setMinimumSize(380, 560)
        self._build()
        self.link.connected.connect(self._connected)
        self.link.disconnected.connect(lambda: self.sb_daemon.setText("Background service · reconnecting…"))
        self.link.message.connect(self._event)
        self.link.open()
        QTimer.singleShot(12_000, self._not_started)
        self._stall = 0.0
        self._tick = time.perf_counter()
        self._lag = QTimer(self, interval=100, timeout=self._lag_check)
        self._lag.start()
        QTimer(self, interval=60_000, timeout=self._report_stall).start()
        QTimer(self, interval=15_000, timeout=self._refresh_times).start()
        self.quitting, self.tray, self.down = False, None, set()
        if QSystemTrayIcon.isSystemTrayAvailable():  # closing the window hides it there; Quit really quits
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            m = QMenu(self)
            self.tray_count = m.addAction("connecting…")
            self.tray_count.setEnabled(False)
            m.addAction("Open Pythonator", self.bring_up)
            m.addAction("Start all", lambda: self.all_services("start"))
            m.addAction("Stop all", lambda: self.all_services("stop"))
            m.addAction("App settings…", lambda: (self.bring_up(), self.app_settings()))
            m.addSeparator()
            m.addAction("Quit", self.quit_app)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self.bring_up() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.show()

    # tray -------------------------------------------------------------------------------------------------------
    def bring_up(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def all_services(self, what: str) -> None:
        """Start every approved service that isn't running, or stop every one that is (the tray's Start/Stop all)."""
        for s in self.services:
            on = s["observed"]["phase"] in ("running", "starting", "backoff")
            if (what == "stop" and on) or (what == "start" and not on and s["approved"] and not s["spec"].get("schedule")):
                self.link.call(what, self.after, name=s["spec"]["name"])

    def quit_app(self) -> None:
        self.quitting = True
        QApplication.quit()

    def app_settings(self) -> None:
        """This app on this computer: switches that take effect at once, and Start all / Stop all."""
        from pythonator.service import osservice
        from pythonator.ui.kit import Popover, Toggle

        pop = Popover("Pythonator on this computer", "Your services keep running in the background service whether or not this window is open.",
                      ico="wrench")
        lay = pop.layout()

        def switch(text: str, on: bool, flip: Any, tip: str = "") -> Toggle:
            t = Toggle(on, tip)
            t.clicked.connect(flip)
            lay.addLayout(hbox(label(text, wrap=True), 1, t, spacing=10))
            return t

        login = switch("Start services at login", False, lambda on: self.link.call(
            "login", lambda r, e: (self.after_err(r, e), login.setChecked(bool(r))), on=on),
            "The background service starts when you log in and starts your services that were running.")
        login.setEnabled(False)
        self.link.call("login", lambda r, e: (login.setChecked(bool(r)), login.setEnabled(not e)))
        switch("Open this window at login, in the tray", osservice.window_at_login(), osservice.window_at_login)
        switch("Closing the window keeps it in the tray", pref("tray"), lambda on: set_pref("tray", on))
        switch("Tell me when a service stops working", pref("notify"), lambda on: set_pref("notify", on))
        lay.addLayout(hbox(Btn("Start all", "play", size="sm", on=lambda: self.all_services("start")),
                           Btn("Stop all", "stop", size="sm", on=lambda: self.all_services("stop")), 1, spacing=8))
        pop.setFixedWidth(380)
        self._app_pop = pop  # kept, or it would be collected while showing
        pop.show_near(self.app_btn)

    def closeEvent(self, e: Any) -> None:
        if self.tray is not None and not self.quitting and pref("tray"):
            e.ignore()
            self.hide()
            self.tray.showMessage("Pythonator", "Pythonator is still running in the system tray, and your services "
                                  "continue to run. Choose Quit from the tray icon to close the application.", self.windowIcon(), 4000)
            return
        super().closeEvent(e)

    # layout -----------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        self.back_top = Btn(ico="back", kind="ghost", on=self.show_list)
        self.back_top.hide()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(RES / "icon-256.png")).scaled(
            22, 22, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.brand = label("Pythonator", "Brand")
        self.search = Search("Search services, files, commands…", "Ctrl K")
        self.search.setMaximumWidth(520)
        self.search.setMinimumWidth(200)
        self.search.edit.installEventFilter(self)
        self.search.edit.textEdited.connect(lambda t: self.palette_open(t))
        self.new_btn = primary("New service", "plus", self.new_service)
        self.bell = Btn(ico="bell", tip="Notifications", on=self.show_notes)
        self.bell_badge = Badge(0, self.bell)
        self.theme_btn = Btn(ico="moon", tip="Light theme", on=self.toggle_theme)
        self.app_btn = Btn(ico="wrench", tip="App settings: login, tray, Start all / Stop all", on=self.app_settings)
        mid = QWidget()
        ml = hbox(1, self.search, 1, spacing=0, margins=(24, 0, 24, 0))
        ml.setStretch(0, 1)
        ml.setStretch(1, 8)
        ml.setStretch(2, 1)
        mid.setLayout(ml)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top = frame("TopBar", hbox(self.back_top, logo, self.brand, mid, self.new_btn, self.bell, self.app_btn, self.theme_btn,
                                   spacing=10, margins=(14, 0, 14, 0)))
        top.layout().setStretch(3, 1)
        top.setFixedHeight(50)
        self.top = top

        self.rail = frame("Rail")
        self.rail.setFixedWidth(56)
        rl = vbox(spacing=6, margins=(9, 12, 9, 12))
        self.rail.setLayout(rl)
        self.rail_group = QButtonGroup(self)
        self.rail_btns: dict[str, QToolButton] = {}
        for key, ico, tip in (*RAIL, ("check", "sliders", "System check")):
            b = QToolButton()
            b.setObjectName("RailBtn")
            b.setCheckable(True)
            b.setFixedSize(38, 38)
            b.setIconSize(QSize(20, 20))
            b.setToolTip(tip)
            b.setProperty("ico", ico)
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            self.rail_group.addButton(b)
            self.rail_btns[key] = b
            if key == "check":
                rl.addStretch(1)
            rl.addWidget(b)
        self.plug_badge = Badge(0, self.rail_btns["plugins"])

        self.pages = QStackedWidget()
        self.services_page = ServicesPage(self)
        self.pages.addWidget(self.services_page)
        self.services_page.insp.allow.connect(self.allow_host)

        self.sb_dot = Dot("idle")
        self.sb_daemon = label("Background service · connecting…")
        self.sb_counts = label("")
        self.sb_res = label("")
        self.sb_plugins = label("")
        self.sb_engines = label("")
        self.sb_current = Chip("all current", "ok", "shield-ok", h=18)
        self.sb_toast = label("", rich=True)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.sb_job = label("")
        self.sb_job.setVisible(False)
        sb = frame("StatusBar", hbox(
            self.sb_dot, self.sb_daemon, 16, Icon("services", "muted", 13), self.sb_counts, 16, Icon("cpu", "muted", 13), self.sb_res,
            1, self.sb_job, 14, self.sb_toast, 14, Icon("plug", "muted", 13), self.sb_plugins, 14, self.sb_engines, 14, self.sb_current,
            spacing=6, margins=(14, 0, 14, 0)))
        sb.setFixedHeight(30)
        self.statusbar = sb
        self._toast_timer = QTimer(self, singleShot=True, interval=12_000, timeout=lambda: self.sb_toast.setText(""))

        self.bnav = frame("BottomNav")
        self.bnav.setFixedHeight(58)
        bl = hbox(spacing=0)
        self.bnav.setLayout(bl)
        self.bnav_btns: dict[str, QToolButton] = {}
        bgroup = QButtonGroup(self)
        for key, ico, text in (("services", "services", "Services"), ("console", "console", "Console"),
                               ("jail", "layers", "Isolation"), ("plugins", "plug", "Plugins"), ("more", "more", "More")):
            b = QToolButton()
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setText(text)
            b.setIconSize(QSize(20, 20))
            b.setProperty("ico", ico)
            b.clicked.connect(lambda _=False, k=key: self._bottom(k))
            bgroup.addButton(b)
            self.bnav_btns[key] = b
            bl.addWidget(b, 1)
        self.bnav.hide()

        body = QWidget()
        body.setLayout(hbox(self.rail, self.pages, spacing=0))
        root = QWidget()
        root.setLayout(vbox(top, body, self.bnav, sb, spacing=0))
        self.setCentralWidget(root)
        for keys, fn in (
            ("Ctrl+K", self.palette_open), ("Ctrl+N", self.new_service), ("Ctrl+R", lambda: self.act("restart")),
            ("Ctrl+T", self.try_copy), ("Ctrl+,", lambda: self.open_settings()), ("Ctrl+Shift+L", self.toggle_theme),
            ("Ctrl+1", lambda: self.go("services")), ("Ctrl+2", lambda: self.go("files")),
            ("Ctrl+3", lambda: self.go("engines")), ("Ctrl+4", lambda: self.go("plugins")),
            ("Ctrl+5", lambda: self.go("security")), ("Ctrl+6", lambda: self.go("check")),
        ):
            QShortcut(QKeySequence(keys), self, activated=fn)
        self.rail_btns["services"].setChecked(True)
        self.retheme()
        self._show_current()

    def retheme(self) -> None:
        t = th.current()
        for b in self.rail_btns.values():
            b.setIcon(icon(str(b.property("ico")), t.text if b.isChecked() else t.muted, 20))
        for b in self.bnav_btns.values():
            b.setIcon(icon(str(b.property("ico")), t.accent_text if b.isChecked() else t.muted, 20))
        self.theme_btn.set_icon("sun" if t is th.LIGHT else "moon")
        self.theme_btn.setToolTip("Dark theme" if t is th.LIGHT else "Light theme")

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self._layout()

    def _layout(self) -> None:
        w = self.width()
        narrow = w < 640
        for x in (self.rail, self.statusbar, self.search, self.bell, self.theme_btn, self.brand):
            show_if(x, not narrow)
        show_if(self.bnav, narrow)
        show_if(self.back_top, narrow and self.show_work and self.pages.currentWidget() is self.services_page)
        self.new_btn.setText("" if narrow else "New service")
        self.new_btn.setProperty("icon_only", narrow)
        self.new_btn.style().polish(self.new_btn)
        self.services_page.layout_for(w, self.show_work)
        for page in self.lazy.values():
            fn = getattr(page, "layout_for", None)
            if fn:
                fn(w)

    def show_list(self) -> None:
        self.show_work = False
        self.go("services")
        self._layout()

    def toggle_drawer(self) -> None:
        insp = self.services_page.insp
        on = not insp.isVisible()
        insp.setProperty("drawer", on)
        if self.width() < 640:  # narrow: the inspector replaces the work pane
            show_if(self.services_page.work, not on)
        show_if(insp, on)

    def _bottom(self, key: str) -> None:
        self.retheme()
        sp = self.services_page
        if key == "services":
            self.show_list()
        elif key == "console":
            self.show_work = True
            self.go("services")
            sp.insp.hide()
            self._layout()
        elif key == "jail":
            self.show_work = True
            self.go("services")
            self._layout()
            sp.work.hide()
            sp.insp.show()
        elif key == "plugins":
            self.go("plugins")
        else:
            m = QMenu(self)
            for k, _ico, text in (*RAIL[1:], ("check", "", "System check")):
                m.addAction(text, lambda k=k: self.go(k))
            m.addAction("Theme", self.toggle_theme)
            m.exec(self.bnav_btns["more"].mapToGlobal(self.bnav_btns["more"].rect().topLeft()))

    # pages ------------------------------------------------------------------------------------------------------
    def page(self, key: str) -> QWidget:
        if key == "services":
            return self.services_page
        if key not in self.lazy:
            if key == "files":
                from pythonator.ui.editor import EditorPage as P
            elif key == "engines":
                from pythonator.ui.engines import EnginesPage as P  # type: ignore[assignment]
            elif key == "plugins":
                from pythonator.ui.plugins import PluginsPage as P  # type: ignore[assignment]
            elif key == "security":
                from pythonator.ui.security import SecurityPage as P  # type: ignore[assignment]
            else:
                from pythonator.ui.syscheck import SystemCheck as P  # type: ignore[assignment]
            w = P(self)
            self.lazy[key] = w
            self.pages.addWidget(w)
            fn = getattr(w, "layout_for", None)
            if callable(fn):
                fn(self.width())
        return self.lazy[key]

    def go(self, key: str) -> None:
        w = self.page(key)
        self.pages.setCurrentWidget(w)
        if key in self.rail_btns:
            self.rail_btns[key].setChecked(True)
        fn = getattr(w, "refresh", None)
        if callable(fn):
            fn()
        self.retheme()

    def show_overlay_page(self, w: QWidget) -> None:
        """A page that belongs to one task (New service, Settings): shown in place, closed with Back/Cancel."""
        self.pages.addWidget(w)
        self.pages.setCurrentWidget(w)

    def close_overlay_page(self, w: QWidget) -> None:
        self.pages.removeWidget(w)
        w.deleteLater()
        self.pages.setCurrentWidget(self.services_page)
        self.rail_btns["services"].setChecked(True)
        self.retheme()

    # data -------------------------------------------------------------------------------------------------------
    def _not_started(self) -> None:
        """Still not connected: say so, with the background service's last logged error, never 'connecting…' forever."""
        if self.link.ready:
            return
        try:
            lines = (self.link.root / "daemon.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-12:]
        except OSError:
            lines = []
        why = "\n".join(lines) or "No error was logged."
        self.sb_daemon.setText("Background service · didn't start (hover for why)")
        self.sb_daemon.setToolTip(why)
        self.toast("bad", "Background service", "didn't start. Last error: " + (lines[-1] if lines else "none logged")
                   + ". The full log is daemon.log in your Pythonator data folder.")
        QTimer.singleShot(30_000, self._not_started)

    def _connected(self) -> None:
        if self.link.daemon_version and self.link.daemon_version != __version__:
            self.toast("warn", "Background service", f"is version {self.link.daemon_version}, this window is "
                       f"{__version__}. Run the installer again to restart it.")
        self.link.call("subscribe", None, topics=["state", "stats", "removed", "bench", "error", "notify", "view",
                                                  "plugin", "traffic", "job"])
        self.snapshot()
        self.plugins_snapshot()

    def plugins_snapshot(self) -> None:
        def got(res: Any, err: Any) -> None:
            if err or not res:
                return
            self.plugins_snap = res
            on = [p for p in res["installed"] if p["enabled"]]
            idle = sum(1 for p in on if p["state"] != "running")
            items = [f"{p['name']}: {p['status']}" for p in on if p.get("status") and p["state"] == "running"]
            self.sb_plugins.setText((" · ".join(items) + " · " if items else "")
                                    + (f"{len(on)} plugins" + (f" ({idle} not running)" if idle else "") if on
                                       else "no plugins"))
            self.sb_plugins.setToolTip("On: " + ", ".join(p["name"] for p in on) if on else "")
            if res.get("safe_mode"):
                self.sb_plugins.setText("plugins: safe mode")
            self.plug_badge.set(sum(1 for p in res["installed"] if p["code_changed"] or p["state"] == "error"))

        self.link.call("plugins.list", got)

    def snapshot(self) -> None:
        self.link.call("snapshot", self._got_snapshot)

    def _got_snapshot(self, res: Any, err: Any) -> None:
        if err or not res:
            return
        self.snap = res
        self.services = res["services"]
        self.jobs = {j["key"]: j for j in res.get("jobs", [])}
        self._show_jobs()
        d = res.get("daemon", {})
        self.sb_dot.set("run")
        self.sb_daemon.setText(f"Background service · up {dur(d.get('uptime', 0))}")
        self.sb_daemon.setToolTip("")
        running = sum(1 for s in self.services if s["observed"]["phase"] in ("running", "starting"))
        down = {s["spec"]["name"] for s in self.services if s["observed"]["phase"] in ("failed", "blocked")}
        failed = len(down)
        if self.tray is not None and down - self.down and pref("notify"):  # a crash, said from the tray too
            self.tray.showMessage("Pythonator", f"{', '.join(sorted(down - self.down))} stopped working.",
                                  QSystemTrayIcon.MessageIcon.Warning, 6000)
        self.down = down
        self.sb_counts.setText(f"{running} running" + (f" · {failed} failed" if failed else ""))
        if self.tray is not None:
            self.tray.setToolTip(f"Pythonator · {self.sb_counts.text()}")
            self.tray_count.setText(f"{running} of {len(self.services)} running" + (f" · {failed} failed" if failed else ""))
        self._status_res()
        host = res.get("host", {})
        eng = " · ".join(f"{k.capitalize()} {'.'.join(v.split('.')[:2])}" for k, v in (host.get("engines") or {}).items() if v)
        self.sb_engines.setText(eng)
        show_if(self.sb_engines, bool(eng))
        stale = host.get("stale", [])
        self.sb_current.set("all current" if not stale else f"{len(stale)} need updates", "ok" if not stale else "warn",
                            "shield-ok" if not stale else "warn")
        self.services_page.list.update_services(self.services)
        drift = len(res.get("drift", []))
        self.bell_badge.set(drift + sum(1 for n in self.notes if not n.get("seen")))
        if not self.current and self.services:
            self.pick(self.services[0]["spec"]["name"])
        elif self.current and not self._svc():
            self.current = ""
            if self.services:
                self.pick(self.services[0]["spec"]["name"])
        self._show_current()
        for page in [*self.lazy.values(), self.services_page.task]:
            fn = getattr(page, "on_snapshot", None)
            if fn:
                fn(res)

    def _show_jobs(self) -> None:
        """Long jobs run in the background service; the window stays usable and shows them here."""
        jobs = list(self.jobs.values())
        if jobs:
            j = jobs[0]
            more = f" (+{len(jobs) - 1})" if len(jobs) > 1 else ""
            self.sb_job.setText(f"⟳ {j['title']}{more}" + (f" · {j['line'][:60]}" if j.get("line") else ""))
            self.sb_job.setToolTip("\n".join(f"{x['title']}: {x.get('line') or 'working…'}" for x in jobs))
        show_if(self.sb_job, bool(jobs))

    def _status_res(self) -> None:
        cpu = sum(s["observed"].get("cpu_percent", 0) for s in self.services)
        mem = sum(s["observed"].get("memory_bytes", 0) for s in self.services) / 1073741824
        self.sb_res.setText(f"{cpu:.0f} % · {mem:.1f} GB")

    def _event(self, topic: str, data: dict[str, Any]) -> None:
        sp = self.services_page
        if topic == "stats":
            sp.list.list.add_sample(data["name"], data["cpu"])
            s = self._svc(data["name"])
            if s:
                s["observed"].update(cpu_percent=data["cpu"], memory_bytes=data["mem"], tasks=data["tasks"])
                if data["name"] == self.current:
                    sp.insp.update_resources(s)
            sp.list.list.update_services(self.services)
            self._status_res()
        elif topic == "log":
            sp.work.console.on_log(data["name"], data["total"])
        elif topic == "job":
            if data.get("done"):
                self.jobs.pop(data["key"], None)
            else:
                self.jobs[data["key"]] = data
            self._show_jobs()
        elif topic in ("state", "removed"):
            self.snapshot()
        elif topic == "traffic":
            rows = self.traffic.setdefault(data["name"], [])
            rows.append(data)
            del rows[:-200]
            if data["name"] == self.current:
                sp.insp.set_traffic(self._svc(), rows)
            if data["verdict"] == "blocked":
                self.toast("bad", data["name"], f"blocked {data['host']}")
        elif topic == "notify":
            self.notes.append({**data, "ts": data.get("ts", time.time())})
            del self.notes[:-200]
            self.toast(data.get("level", "info"), data["plugin"], data["text"])
            self._notes_changed()
        elif topic == "view" and data.get("service") == self.current:
            self._load_views()
        elif topic == "plugin":
            self.plugins_snapshot()
            w = self.lazy.get("plugins")
            if w is not None and self.pages.currentWidget() is w:
                w.refresh()  # type: ignore[attr-defined]

    def _svc(self, name: str | None = None) -> dict[str, Any] | None:
        name = name or self.current
        return next((s for s in self.services if s["spec"]["name"] == name), None)

    def pick(self, name: str) -> None:
        self.show_work = True
        if self.pages.currentWidget() is not self.services_page:
            self.go("services")
        task = self.services_page.task
        if task is not None and getattr(task, "name", "") != name:
            self.services_page.close_task()
        if name == self.current:
            self._layout()
            return
        self.current = name
        sp = self.services_page
        sp.list.list.select(name)
        self.link.call("watch_logs", None, names=[name])
        s = self._svc()
        sp.work.console.show_service(name, s["log_total"] if s else 0, self._header(s))
        self.link.call("traffic", self._got_traffic, name=name)
        self._show_current()
        self._load_views()
        self._layout()

    def _got_traffic(self, res: Any, err: Any) -> None:
        if not err and res is not None:
            name = res[0]["name"] if res else self.current
            self.traffic[name] = list(res)
            if name == self.current:
                self.services_page.insp.set_traffic(self._svc(), self.traffic[name])

    def _header(self, s: dict[str, Any] | None) -> list[str]:
        if not s or not s.get("plan"):
            return []
        plan = s["plan"]
        layers = {x["key"]: x for x in plan["layers"]}
        by = [x["by"].split(" (")[0] for k in ("tree", "files", "syscalls", "limits") if (x := layers.get(k)) and x["status"] != "off"]
        lim = s["spec"]["limits"]
        line1 = "Pythonator · " + " · ".join([plan["mode"].capitalize(), *dict.fromkeys(by)]) + \
            f" {lim['memory_mb']} MB / {lim['cpu']:g} CPU / {lim['tasks']}"
        net = s["spec"]["network"]
        traffic = {"off": "off", "hosts": f"allowed hosts ({len(net['traffic']['hosts'])}) via gate", "any": "anywhere"}[
            net["traffic"]["mode"]]
        ports = ", ".join(str(p["port"]) for p in net["ports"]) or "none"
        n = layers.get("network")
        line2 = f"Pythonator · Network · {n['by'] if n else 'host'} · ports: {ports} · traffic: {traffic}"
        return [line1, line2]

    def _show_current(self) -> None:
        s = self._svc()
        sp = self.services_page
        sp.work.show_service(s)
        sp.insp.show_service(s, (self.snap.get("host") or {}).get("os", sys.platform))
        sp.insp.set_traffic(s, self.traffic.get(self.current, []))
        notes = [n for n in self.notes if not self.current or n.get("service") in (None, "", self.current)]
        sp.insp.set_notes(notes)
        sp.work.set_notify([n for n in self.notes if n.get("service") == self.current])
        sp.work.console.set_header(self._header(s))
        if s:  # a log line it missed (it was hidden, or started before it watched) shows up at the next snapshot
            sp.work.console.on_log(self.current, s.get("log_total", 0))
        n_blocked = len({e["host"] for e in self.traffic.get(self.current, []) if e["verdict"] == "blocked"})
        sp.work.tabs.set_count("network", n_blocked)

    def _load_views(self) -> None:
        name = self.current

        def got(res: Any, err: Any) -> None:
            if not err and name == self.current:
                self.services_page.insp.set_views([(p, r) for p, rs in (res or {}).items() for r in rs])

        self.link.call("plugins.views", got, service=name)

    def _refresh_times(self) -> None:
        self.services_page.list.list.viewport().update()

    def _notes_changed(self) -> None:
        self.bell_badge.set(len(self.snap.get("drift", [])) + sum(1 for n in self.notes if not n.get("seen")))
        self._show_current()

    def toast(self, level: str, who: str, text: str) -> None:
        t = th.current()
        col = {"warn": t.warn, "error": t.bad, "bad": t.bad}.get(level, t.accent_text)

        self.sb_toast.setText(f"<span style='color:{col}'>●</span> <b>{escape(who[:60])}</b> · {escape(text[:240])}")
        self.sb_toast.setToolTip(text)
        self._toast_timer.start()

    # actions ----------------------------------------------------------------------------------------------------
    def after_err(self, _r: Any, err: Any) -> None:
        if err:
            QMessageBox.warning(self, "Pythonator", err["message"] + (f"\n\nFix: {err['fix']}" if err.get("fix") else ""))

    def after(self, r: Any, err: Any) -> None:
        self.after_err(r, err)
        self.snapshot()

    def act(self, what: str, name: str | None = None) -> None:
        name = name or self.current
        s = self._svc(name)
        if not s:
            return
        if what == "start" and not s["approved"]:
            self.show_plan(name)
            return
        self.link.call(what, self.after, name=name)

    def tab(self, key: str) -> None:
        sp = self.services_page
        if key == "console":
            sp.work.pages.setCurrentIndex(0)
            sp.work.console.refresh()
        elif key == "notify":
            sp.work.pages.setCurrentIndex(1)
        elif key == "editor":
            sp.work.tabs.select("console")
            self.go("files")
            ed = self.lazy["files"]
            ed.open_service(self.current)  # type: ignore[attr-defined]
        else:
            sp.work.tabs.select("console")
            self.open_settings(section="network" if key == "network" else "general")
        sp.work.tabs.retheme()

    def new_service(self) -> None:
        from pythonator.ui.wizard import NewService

        self.show_overlay_page(NewService(self))

    def open_settings(self, name: str | None = None, section: str = "general") -> None:
        s = self._svc(name)
        if not s:
            return
        from pythonator.ui.settings import SettingsPage

        self.show_overlay_page(SettingsPage(self, s, section))

    def show_plan(self, name: str | None = None) -> None:
        s = self._svc(name)
        if not s:
            return
        self._open_plan(s)

    def _open_plan(self, s: dict[str, Any] | None, inbox: bool = False, try_now: bool = False) -> None:
        from pythonator.ui.plan import PlanPage

        if self.pages.currentWidget() is not self.services_page:
            self.go("services")
        self.show_work = True
        self.services_page.show_task(PlanPage(self, s, inbox=inbox, try_now=try_now))
        self._layout()

    def try_copy(self) -> None:
        s = self._svc()
        if s:
            self._open_plan(s, try_now=True)

    def show_notes(self) -> None:
        for n in self.notes:
            n["seen"] = True
        self.bell_badge.set(len(self.snap.get("drift", [])))
        self._open_plan(self._svc(), inbox=True)

    def allow_host(self, host: str) -> None:
        self.link.call("allow_host", self.after, name=self.current, host=host)

    def remove_service(self) -> None:
        s = self._svc()
        if s and QMessageBox.question(
            self, "Remove", f"Stop and remove {self.current}? Its folder is not touched, and every grant it had is undone."
        ) == QMessageBox.StandardButton.Yes:
            name, self.current = self.current, ""
            self.link.call("remove", self.after, name=name)

    def rename_service(self) -> None:
        old = self.current
        new, ok = QInputDialog.getText(self, "Rename", f"New name for {old} (stop it first):", text=old)
        if ok and new.strip() and new.strip() != old:
            self.link.call("rename", lambda r, e: (self.after(r, e), None if e else self.pick(new.strip().lower())),
                           name=old, new=new)

    def save_log(self) -> None:
        name = self.current
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save log", str(Path.home() / f"{name}.log"))
        if not path:
            return

        def got(res: Any, err: Any) -> None:
            if err:
                self.after_err(None, err)
                return
            Path(path).write_text("\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x[1] / 1000))} {x[3]}"
                                            for x in res["lines"]) + "\n", encoding="utf-8")
            self.toast("info", name, f"saved {len(res['lines'])} lines to {path}")

        self.link.call("logs", got, name=name, start=-1, count=100_000)

    def toggle_theme(self) -> None:
        t = th.LIGHT if th.current() is th.DARK else th.DARK
        th.apply(QApplication.instance(), t)  # type: ignore[arg-type]
        th.remember(t)
        retheme(self)
        for w in self.lazy.values():  # pages that tint their own parts rebuild with the new tokens
            fn = getattr(w, "_build", None)
            if callable(fn):
                fn()

    def palette_open(self, text: str = "") -> None:
        from pythonator.ui.palette import Palette

        if getattr(self, "_palette", None) and self._palette.isVisible():
            return
        self._palette = Palette(self, text if isinstance(text, str) else "")
        self.search.edit.clear()
        self._palette.show()

    def eventFilter(self, obj: Any, e: Any) -> bool:
        from PyQt6.QtCore import QEvent

        if obj is self.search.edit and e.type() == QEvent.Type.MouseButtonPress:
            self.palette_open()
            return True
        return super().eventFilter(obj, e)

    # speed ------------------------------------------------------------------------------------------------------
    def paintEvent(self, e: Any) -> None:
        super().paintEvent(e)
        if not self.painted:
            self.painted = True
            ms = self.t0.elapsed()
            QTimer.singleShot(2000, lambda: self._first_paint(ms))

    def _first_paint(self, ms: int) -> None:
        self.link.call("report_ui", None, first_paint_ms=ms)
        self._stall = 0.0

    def _lag_check(self) -> None:
        now = time.perf_counter()
        late = (now - self._tick - 0.1) * 1000
        self._tick = now
        self._stall = max(self._stall, late)

    def _report_stall(self) -> None:
        if self._stall > 0:
            self.link.call("report_ui", None, ui_stall_ms=round(self._stall, 1))
            self._stall = 0.0

class _PlainTips(QObject):
    """Every tooltip is shown as plain text. Qt guesses rich text from a leading tag, and tooltips carry service,
    plugin and host text (an image name, a refusal reason), so a guess could render markup or load a local image."""

    def eventFilter(self, obj: QObject | None, ev: QEvent | None) -> bool:  # noqa: N802 (Qt's name)
        if ev is not None and ev.type() == QEvent.Type.ToolTip and isinstance(obj, QWidget) and obj.toolTip():
            text = escape(obj.toolTip()).replace("\n", "<br>")
            QToolTip.showText(ev.globalPos(), f"<qt>{text}</qt>", obj)
            return True
        return False

class _Bridge(QObject):
    """Carries a message from any thread to the GUI thread (a queued signal)."""

    said = pyqtSignal(str)

def pref(key: str) -> bool:
    """The app settings' switches, all on unless turned off."""
    return QSettings("Pythonator", "Pythonator").value(f"app/{key}", "on") == "on"

def set_pref(key: str, on: bool) -> None:
    QSettings("Pythonator", "Pythonator").setValue(f"app/{key}", "on" if on else "off")

def main() -> int:
    t0 = QElapsedTimer()
    t0.start()
    if sys.platform == "win32":  # our own taskbar icon and grouping, not python.exe's
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("io.github.cfunkz.pythonator")
    app = QApplication(sys.argv)
    app.setApplicationName("Pythonator")
    app.setApplicationVersion(__version__)
    tips = _PlainTips(app)
    app.installEventFilter(tips)
    app.setStyle("Fusion")  # the same base style on every OS, so the style sheet looks the same everywhere
    th.apply(app, th.remembered())
    import signal

    from pythonator.safe import process

    root = data_dir()
    process.harden(root)
    w = Main(t0)
    # An exception in any slot or thread is logged and shown as a toast; the window keeps running. The toast is
    # queued to the GUI thread, since the hook can fire on a worker thread.
    bridge = _Bridge(w)
    bridge.said.connect(lambda text: w.toast("error", "Pythonator", text))
    process.install_hook(root / "window.log", bridge.said.emit)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # the window is only a client: a closed terminal detaches it
    if "--tray" not in sys.argv or w.tray is None:  # opened at login: only the tray icon
        w.show()
    return app.exec()
