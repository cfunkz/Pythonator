"""Screen 8: Engines. Podman, Docker and WSL on one page, with the read-only command each card runs, the
containers on each engine and who owns them (Pythonator's ownership ledger), and the runtimes Pythonator can use.
Only ledger rows can be restarted or pruned from here. Anything else on the engine is shown as "not Pythonator" and
left alone."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QInputDialog,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from pythonator.ui import theme as th
from pythonator.ui.kit import Btn, Chip, Icon, InfoBtn, Panel, clear, esc, frame, hbox, label, rebuild_when_narrow, vbox
from pythonator.ui.widgets import PHASE_TEXT, PHASE_TONE

if TYPE_CHECKING:
    from pythonator.ui.app import Main

ENGINE = {
    "podman": ("Podman", "box", "#5b8def"),
    "docker": ("Docker", "ship", "#2496ed"),
    "wsl": ("WSL", "console", "#c77dff"),
}
LEDGER_INFO = (
    "Which containers Pythonator manages",
    "Pythonator writes every container, network and volume it creates to its ownership ledger before creating "
    "it. Only those rows can be stopped or pruned from here; anything else on the engine is shown as “not "
    "Pythonator” and left alone.",
    [("After a crash", "ledger finishes or undoes half-done steps"), ("Uninstall", "removes exactly what is in the ledger")],
    "Uninstall leaves nothing behind",
)
DOCKER_ROOT = (
    "Docker runs as root",
    "Docker's daemon runs as root, so anyone who can use its socket is root on this computer. Pythonator never adds "
    "you to the docker group. Restart restarts Pythonator's own containers on Docker, never the daemon. Podman runs "
    "rootless, which is why it's recommended.",
    [("Socket", "/var/run/docker.sock"), ("Rootless", "docs.docker.com/engine/security/rootless")],
    "",
)

def _msg(err: Any) -> str:
    return str(err.get("message", err)) if isinstance(err, dict) else str(err)

def human(n: int) -> str:
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"

def engine_tile(key: str, size: int = 44) -> QFrame:
    _name, ico, color = ENGINE[key]
    f = QFrame()
    f.setFixedSize(size, size)
    f.setObjectName("Tile")
    f.setStyleSheet(f"#Tile {{ background: {th.rgba(color, 0.16)}; border-radius: {th.RCARD}px; }}")
    i = Icon(ico, "text", 24)
    i.setStyleSheet(f"color: {color};")
    from pythonator.ui.icons import icon

    i.setPixmap(icon(ico, color, 24).pixmap(24, 24))
    lay = vbox(i)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    f.setLayout(lay)
    return f

def cmd_box(text: str) -> QFrame:
    lbl = label(f"$ {text}", "Mono")
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    lbl.setToolTip(text)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setStyleSheet("font-size: 12.5px;")
    f = frame("Console", hbox(lbl, margins=(13, 10, 13, 10)))
    return f

def kv_grid(rows: list[tuple[str, str, str]]) -> QGridLayout:
    g = QGridLayout()
    g.setHorizontalSpacing(12)
    g.setVerticalSpacing(9)
    for r, (k, v, tone) in enumerate(rows):
        g.addWidget(label(k, "Muted"), r, 0)
        val = label(v, "Mono", wrap=True)
        val.setStyleSheet("font-size: 12.5px;" + (f"color: {th.tone(tone)};" if tone else ""))
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        g.addWidget(val, r, 1)
    g.setColumnStretch(1, 1)
    return g

class PropTable(QTableWidget):
    """A table whose columns keep set shares of the width (the last one takes the rest)."""

    def __init__(self, headers: list[str], shares: tuple[float, ...]) -> None:
        super().__init__(0, len(headers))
        self.shares = shares
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)  # type: ignore[union-attr]
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        hh = self.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)  # type: ignore[union-attr]
        hh.setStretchLastSection(True)  # type: ignore[union-attr]

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        w = self.viewport().width()  # type: ignore[union-attr]
        for i, sh in enumerate(self.shares):
            self.setColumnWidth(i, int(w * sh))

class EnginesPage(QScrollArea):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.main = main
        self.link = main.link
        self.setObjectName("Page")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.data: dict[str, Any] = {}
        self.rt: dict[str, Any] = {}
        self.busy = False
        self.body = QWidget()
        self.body.setObjectName("Page")
        self.setWidget(self.body)
        self.lay = vbox(spacing=16, margins=(26, 20, 26, 26))
        self.body.setLayout(self.lay)
        self._build()

    # data -------------------------------------------------------------------------------------------------------
    def refresh(self, recheck: bool = False) -> None:
        self.link.call("engines.list", self._got, recheck=recheck)
        self.link.call("runtime.list", self._got_rt)

    def _got(self, res: Any, err: Any) -> None:
        self.busy = False
        if err:
            self.data = {"error": _msg(err)}
        elif isinstance(res, dict):
            self.data = res
        self._build()

    def _got_rt(self, res: Any, err: Any) -> None:
        if not err and isinstance(res, dict):
            self.rt = res
            self._build()

    def check_updates(self) -> None:
        self.busy = True
        self._build()
        self.refresh(recheck=True)

    def restart(self, engine: str) -> None:
        def done(res: Any, err: Any) -> None:
            if err:
                self.main.toast("bad", "Engines", _msg(err))
            else:
                n = len(res.get("restarted", []))
                self.main.toast("ok", "Engines", f"Restarted {n} service{'s' if n != 1 else ''} on {ENGINE[engine][0]}."
                                if n else f"No Pythonator service is running on {ENGINE[engine][0]}.")
            self.refresh()

        self.link.call("engines.restart", done, engine=engine)

    def prune(self, engine: str) -> None:
        name = ENGINE[engine][0]
        ok = QMessageBox.question(
            self, f"Prune Pythonator's on {name}",
            f"Remove stopped containers and unused images that Pythonator's ledger lists on {name}?\n\n"
            "Nothing that isn't in the ledger is touched.")
        if ok != QMessageBox.StandardButton.Yes:
            return

        def done(res: Any, err: Any) -> None:
            if err:
                self.main.toast("bad", "Engines", _msg(err))
            else:
                n = len(res.get("removed", []))
                self.main.toast("ok", "Engines", f"Pruned {n} item{'s' if n != 1 else ''} from {name}." if n else
                                f"Nothing of Pythonator's to prune on {name}.")
            self.refresh()

        self.link.call("engines.prune", done, engine=engine)

    def setup_wsl(self) -> None:
        self.main.toast("info", "WSL", "Setting up Pythonator's own Debian (about 100 MB). This takes a few minutes.")

        def done(res: Any, err: Any) -> None:
            self.main.toast("bad" if err else "ok", "WSL", _msg(err) if err else str(res))
            self.refresh(recheck=True)

        self.link.call("wsl.setup", done)

    def install_python(self) -> None:
        v, ok = QInputDialog.getText(self, "Install a Python", "Version (for example 3.13):")
        if not ok or not v.strip():
            return

        def done(res: Any, err: Any) -> None:
            self.main.toast("bad" if err else "ok", "Runtimes", _msg(err) if err else str(res))
            self.refresh()

        self.link.call("runtime.install", done, version=v.strip())

    # layout -----------------------------------------------------------------------------------------------------
    def _clear(self) -> None:
        clear(self.lay)

    def _build(self) -> None:
        self._clear()
        narrow = 50 < self.width() < 700
        self.lay.setContentsMargins(*((12, 14, 12, 18) if narrow else (26, 20, 26, 26)))
        d = self.data
        upd = Btn(ico="restart", on=self.check_updates,
                  tip="Check engines again: asks each one for its version and compares it to the CVE floor")
        upd.setEnabled(not self.busy)
        info = InfoBtn("Engines", "Podman and Docker run Container mode; WSL runs WSL mode on Windows. Each card shows "
                       "the read-only command it runs to fill itself in, so you can run it yourself.")
        sub = label("Docker and Podman here; WSL on Windows. Every action shows the command it runs.", "Muted")
        sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head = hbox(label("Engines", "H1"), info, sub, upd, spacing=10)
        head.setStretch(2, 1)
        self.lay.addLayout(head)
        if d.get("error"):
            self.lay.addWidget(label(esc(d["error"]), "Muted"))
        cards = {c["engine"]: c for c in d.get("engines", [])}
        row = QGridLayout()
        row.setHorizontalSpacing(16)
        row.setVerticalSpacing(16)
        self.cards = [self._engine_card(cards.get("podman", {"engine": "podman"})),
                      self._engine_card(cards.get("docker", {"engine": "docker"})),
                      self._wsl_card(d.get("wsl") or {})]
        self._place(row)
        self.row = row
        self.lay.addLayout(row)
        self.lay.addWidget(self._containers(d.get("containers", [])))
        self.lay.addWidget(self._runtimes())
        self.lay.addStretch(1)

    def _place(self, row: QGridLayout) -> None:
        cols = 3 if self.width() >= 980 or self.width() < 50 else 1
        for i, c in enumerate(self.cards):
            row.addWidget(c, i // cols, i % cols)
        for i in range(3):
            row.setColumnStretch(i, 1 if i < cols else 0)

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        rebuild_when_narrow(self)
        tbl = getattr(self, "_tbl", None)
        if tbl is not None:
            narrow = self.width() < 700
            tbl.setColumnHidden(1, narrow)  # engine
            tbl.setColumnHidden(4, narrow)  # owner (the tooltip on the row still says it)
            tbl.shares = (0.3, 0.0, 0.42, 0.28) if narrow else (0.17, 0.11, 0.31, 0.14)
        if hasattr(self, "row"):
            want = 3 if self.width() >= 980 else 1
            have = sum(1 for i in range(3) if self.row.columnStretch(i))
            if want != have:
                for c in self.cards:
                    self.row.removeWidget(c)
                self._place(self.row)

    def _head(self, key: str, sub: str, chip: Chip | None) -> QWidget:
        name = label(ENGINE[key][0], "H2")
        s = label(sub, "Small")
        return frame("", hbox(engine_tile(key), vbox(name, s, spacing=1), 1, chip, spacing=12))

    def _engine_card(self, c: dict[str, Any]) -> QWidget:
        key = c["engine"]
        card = frame("Panel")
        m = 14 if 50 < self.width() < 700 else 20
        lay = vbox(spacing=12, margins=(m, 18, m, 18))
        card.setLayout(lay)
        if not c.get("installed"):
            lay.addWidget(self._head(key, "not installed", Chip("not installed", "line")))
            tip = ("Podman runs rootless: recommended for Container mode." if key == "podman" else
                   "Docker works too; its daemon runs as root.")
            floor = {"podman": "4.9.4", "docker": "25.0.5"}[key]
            lay.addLayout(kv_grid([
                ("Containers", "—", ""), ("CVE floor", f"≥ {floor}", ""),
                ("Install", "podman.io" if key == "podman" else "docs.docker.com/engine/install", ""),
            ]))
            lay.addWidget(label(tip, "Small", wrap=True))
            lay.addWidget(cmd_box(f"{key} --version"))
            lay.addStretch(1)
            return card
        mode = "rootless" if c.get("rootless") else "rootful" if c.get("rootless") is False else "unknown"
        sub = f"{c.get('version') or '?'} · {mode}"
        if not c.get("reachable"):
            chip = Chip("not running", "warn", "warn")
        elif not c.get("ok"):
            chip = Chip("below CVE floor", "bad", "warn")
        elif c.get("recommended"):
            chip = Chip("recommended", "acc", "star")
        else:
            chip = Chip("running", "ok", "pulse")
        lay.addWidget(self._head(key, sub, chip))
        floor = c.get("floor", "")
        if c.get("reachable"):
            rows = [
                ("Containers", f"{c.get('containers', 0)} · {c.get('mine', 0)} by Pythonator", ""),
                ("Images", f"{c.get('images', 0)} · {human(int(c.get('image_bytes', 0)))}", ""),
                ("CVE floor", f"≥ {floor} " + ("✓" if c.get("ok") else "✗"), "" if c.get("ok") else "bad"),
                ("Socket", c.get("socket") + (", see (i)" if key == "docker" and not c.get("rootless") else ""), ""),
            ]
        else:
            rows = [("Version", c.get("version") or "?", ""), ("CVE floor", f"≥ {floor}", ""),
                    ("Status", (c.get("error") or "didn't answer")[:60], "warn")]
        g = kv_grid(rows)
        if not c.get("ok") and c.get("floor_why"):
            g.itemAtPosition(2, 0).widget().setToolTip(c["floor_why"])  # type: ignore[union-attr]
        lay.addLayout(g)
        lay.addWidget(cmd_box(c.get("command", "")))
        btns = hbox(Btn(ico="restart", size="sm", on=lambda k=key: self.restart(k),
                        tip=f"Restart Pythonator's own services on {ENGINE[key][0]} (not the engine)"), spacing=8)
        if key == "podman" or c.get("rootless"):
            btns.addWidget(Btn(ico="trash", kind="ghost", size="sm", on=lambda k=key: self.prune(k),
                               tip="Prune: removes only the containers and images Pythonator's ledger lists"))
        else:
            btns.addWidget(InfoBtn(*DOCKER_ROOT))
        btns.addStretch(1)
        lay.addLayout(btns)
        return card

    def _wsl_services(self) -> list[dict[str, Any]]:
        return [s for s in self.main.services if s["spec"]["mode"] == "wsl"]

    def on_snapshot(self, _res: dict[str, Any]) -> None:
        """A WSL service started or stopped: redraw its row (containers come from the engines, on refresh)."""
        sig = [(s["spec"]["name"], s["observed"]["phase"]) for s in self._wsl_services()]
        if sig != getattr(self, "_wsl_sig", sig) and self.data:
            self._build()
        self._wsl_sig = sig

    def _wsl_card(self, w: dict[str, Any]) -> QWidget:
        card = frame("Panel")
        lay = vbox(spacing=12, margins=(20, 18, 20, 18))
        card.setLayout(lay)
        windows = w.get("os") == "windows"
        if windows:
            sub = "Pythonator's distro ready" if w.get("ready") else "installed · not set up" if w.get("exe") else "not installed"
            chip = Chip("ready", "ok", "check") if w.get("ready") else Chip("set up", "warn")
        else:
            sub, chip = "not on this machine", Chip("Windows only", "line")
        lay.addWidget(self._head("wsl", sub, chip))
        osname = {"linux": "Linux", "darwin": "macOS", "windows": "Windows"}.get(str(w.get("os")), str(w.get("os", "")))
        rows = [
            ("This machine", f"{osname} · " + ("available" if windows and w.get("exe") else "not available"), ""),
            ("On Windows", "own Debian distro, interop off", ""), ("Drives", "never mounted", ""),
            ("Here, use", "WSL, Sandbox or Container" if windows else "Sandbox or Container", ""),
        ]
        lay.addLayout(kv_grid(rows))
        lay.addWidget(cmd_box(w.get("command") or "# shown on Windows 10 and 11"))
        if windows and not w.get("ready"):
            lay.addLayout(hbox(Btn("Set up", "download", size="sm", on=self.setup_wsl,
                                   tip="Installs Debian from Microsoft's catalog, then turns off drives and interop"), 1))
        lay.addStretch(1)
        return card

    def _containers(self, rows: list[dict[str, Any]]) -> QWidget:
        info = InfoBtn(*LEDGER_INFO[:2], LEDGER_INFO[2], LEDGER_INFO[3], ico="list")
        legend = Chip("Pythonator-managed = in its ledger", "line", "list")
        p = Panel("Services on engines", "box", info=info, right=[legend], pad=14)
        # WSL services run as processes in Pythonator's distro, not as containers; listed with them all the same
        rows = [{"service": s["spec"]["name"], "engine": "wsl", "image": "Pythonator's Debian · bubblewrap", "ledger": True,
                 "state": PHASE_TEXT.get(ph, ph).lower(), "tone": PHASE_TONE.get(ph, ""), "owner": "Pythonator · WSL mode"}
                for s in self._wsl_services() if (ph := s["observed"]["phase"])] + rows
        t = th.current()
        tbl = PropTable(["SERVICE", "ENGINE", "IMAGE (PINNED)", "STATE", "OWNER"], (0.17, 0.11, 0.31, 0.14))
        self._tbl = tbl
        if self.width() < 700 and self.width() > 50:
            tbl.setColumnHidden(1, True)
            tbl.setColumnHidden(4, True)
            tbl.shares = (0.3, 0.0, 0.42, 0.28)
        for r in rows:
            i = tbl.rowCount()
            tbl.insertRow(i)
            tbl.setRowHeight(i, 52)
            mine = bool(r.get("ledger"))
            svc = QTableWidgetItem(r.get("service") or "—")
            f = th.ui_font(13, 600)
            svc.setFont(f)
            svc.setToolTip(f"{r.get('owner', '')} · {r.get('engine', '')}")
            tbl.setItem(i, 0, svc)
            eng = QTableWidgetItem(ENGINE.get(r["engine"], (r["engine"],))[0])
            eng.setFont(th.ui_font(13))
            tbl.setItem(i, 1, eng)
            img = r.get("image", "")
            if not mine:
                img = f"{r.get('name') or img} (another app)"
            elif "@sha256:" in img:
                base, _, dig = img.partition("@sha256:")
                img = f"{base}@sha256:{dig[:4]}…"
            im = QTableWidgetItem(img)
            im.setFont(th.mono_font(12.5))
            im.setToolTip(r.get("image", ""))
            tbl.setItem(i, 2, im)
            tone = r.get("tone", "")
            chip = Chip(r.get("state", ""), {"ok": "ok", "warn": "warn", "bad": "bad"}.get(tone, "line"))
            tbl.setCellWidget(i, 3, frame("", hbox(chip, 1, margins=(6, 0, 0, 0))))
            own = QTableWidgetItem(r.get("owner", ""))
            own.setForeground(QColor(t.muted))
            own.setFont(th.ui_font(12.5))
            if r.get("labelled") and not mine:
                own.setToolTip("It carries a pythonator label, but the ledger doesn't list it, so it is left alone.")
            tbl.setItem(i, 4, own)
        tbl.setFixedHeight(42 + 52 * tbl.rowCount())
        tbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if rows:
            p.box.setContentsMargins(0, 0, 0, 0)
            p.box.addWidget(tbl)
        else:
            installed = any(c.get("reachable") for c in self.data.get("engines", []))
            p.box.addWidget(label("No containers or WSL services yet. Container and WSL mode services show up here." if installed
                                  else "No engine is running, so there are no containers to show.", "Muted"))
        return p

    def _runtimes(self) -> QWidget:
        p = Panel("Runtimes", "code", info=InfoBtn(
            "Runtimes", "The languages Pythonator found on this computer, by trusted path. Python versions are "
            "installed by uv into Pythonator's own folder, never over your system Python.",
            [("Python", "uv python install, checksummed"), ("Others", "your package manager")]),
            right=[Btn("Install a Python…", "download", size="sm", on=self.install_python)])
        rts = self.rt.get("runtimes", [])
        if not rts:
            p.box.addWidget(label("Looking for runtimes…", "Muted"))
            return p
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(8)
        cols = 2 if self.width() >= 900 or self.width() < 50 else 1
        for i, r in enumerate(rts):
            found = bool(r.get("path"))
            dot = Icon("check" if found else "x", "ok" if found else "faint", 13)
            name = label(r["label"], "H3" if found else "Muted")
            where = label(r["path"] if found else r.get("install", ""), "MonoMuted")
            where.setToolTip(r.get("install", ""))
            where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            cell = hbox(dot, name, where, spacing=8)
            cell.setStretch(2, 1)
            name.setFixedWidth(110)
            g.addLayout(cell, i // cols, i % cols)
        p.box.addLayout(g)
        py = self.rt.get("python", [])
        if py:
            p.box.addWidget(label("Pythons installed by uv: " + ", ".join(esc(x["version"]) for x in py[:8]), "Small"))
        return p
