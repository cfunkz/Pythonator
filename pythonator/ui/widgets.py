"""The service list (groups, filters, rings and live lines) and the inspector's security stack and traffic log."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QModelIndex, QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from pythonator.ui import theme as th
from pythonator.ui.icons import pixmap
from pythonator.ui.kit import LAYER_TONE, paint_dot, paint_rings, paint_spark

MODE = {
    "sandbox": ("Sandbox", "shield", "m_sandbox"), "container": ("Container", "box", "m_container"),
    "wsl": ("WSL", "monitor", "m_wsl"), "full": ("Full access", "unlock", "m_full"),
}
PHASE_DOT = {"running": "run", "starting": "run", "backoff": "warn", "blocked": "warn", "failed": "bad", "stopping": "idle"}
PHASE_TEXT = {
    "running": "Running", "starting": "Starting", "stopping": "Stopping", "stopped": "Stopped", "backoff": "Restarting",
    "failed": "Gave up", "blocked": "Blocked",
}
PHASE_TONE = {"running": "ok", "starting": "info", "backoff": "warn", "blocked": "warn", "failed": "bad"}
ROLE_SVC, ROLE_NAME, ROLE_GROUP = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1, Qt.ItemDataRole.UserRole + 2

def dur(s: float) -> str:
    s = int(max(0, s))
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 60}m" if s >= 60 else f"{s}s"

def mode_label(spec: dict[str, Any]) -> tuple[str, str, str]:
    text, ico, tone = MODE.get(spec.get("mode", "sandbox"), MODE["sandbox"])
    if spec.get("mode") == "container":
        eng = (spec.get("container") or {}).get("engine", "auto")
        text = {"podman": "Podman", "docker": "Docker"}.get(eng, "Container")
    return text, ico, tone

def meta(s: dict[str, Any]) -> str:
    """The grey line next to the mode chip: up 3h 12m · gave up · retry in 9 m · cron 0 3 * * * · next 02:59."""
    spec, ob = s["spec"], s["observed"]
    ph = ob.get("phase", "stopped")
    if s.get("refused"):
        return f"refused · {s['refused']['code']}"
    if not s.get("approved"):
        return "awaiting approval"
    if ph == "running" and ob.get("started_at"):
        return f"up {dur(time.time() - ob['started_at'])}"
    if ph == "backoff" and ob.get("next_retry_at"):
        return f"restarting · in {dur(ob['next_retry_at'] - time.time())}"
    if ph == "failed":
        return f"gave up · {ob.get('message') or 'crashed'}"
    if spec.get("schedule"):
        nxt = s.get("next_run")
        return f"cron {spec['schedule']['cron']}" + (f" · next {time.strftime('%H:%M', time.localtime(nxt))}" if nxt else "")
    return ob.get("message") or PHASE_TEXT.get(ph, ph).lower()

def layer_states(s: dict[str, Any]) -> list[str]:
    if s.get("refused"):
        return ["not-enforced"]
    return [x["status"] for x in (s.get("plan") or {}).get("layers", []) if x["key"] in ("tree", "files", "network", "limits")]

def needs_you(s: dict[str, Any]) -> bool:
    return bool(not s.get("approved") or s.get("refused") or s["observed"].get("phase") in ("failed", "blocked"))

# --- service list -----------------------------------------------------------------------------------------------
class ServiceDelegate(QStyledItemDelegate):
    H, GH = 90, 28

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(260, self.GH if index.data(ROLE_GROUP) else self.H)

    def paint(self, p: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if p is None:
            return
        t = th.current()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = index.data(ROLE_GROUP)
        if group:
            p.setFont(th.label_font(10))
            p.setPen(QColor(t.faint))
            p.drawText(QRect(option.rect).adjusted(18, 8, -8, 0), Qt.AlignmentFlag.AlignVCenter, group)
            p.restore()
            return
        s: dict[str, Any] = index.data(ROLE_SVC) or {}
        spec, ob = s.get("spec", {}), s.get("observed", {})
        r = QRectF(option.rect).adjusted(8, 2, -8, -2)
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        hov = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if sel or hov:
            p.setPen(QPen(QColor(t.border_strong if sel else t.border), 1))
            p.setBrush(QColor(t.raised2 if sel else t.raised))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), th.RC, th.RC)
        x0, y0 = r.left() + 10, r.top() + 9
        paint_rings(p, QPointF(x0 + 18, y0 + 22), layer_states(s), 16)
        tx = int(x0 + 46)
        p.setFont(th.ui_font(13.5, 600))
        p.setPen(QColor(t.text))
        fm = QFontMetrics(p.font())
        p.drawText(QRect(tx, int(y0), int(r.right() - tx - 30), 20), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(spec.get("name", "?"), Qt.TextElideMode.ElideRight, int(r.right() - tx - 30)))
        ph = ob.get("phase", "stopped")
        paint_dot(p, QPointF(r.right() - 16, y0 + 10), "off" if ph == "stopped" else PHASE_DOT.get(ph, "idle"))
        cy = int(y0 + 24)
        text, ico, tone = mode_label(spec)
        x = tx + chip(p, tx, cy, text, tone, ico) + 6
        for port in (spec.get("network") or {}).get("ports", [])[:1]:
            label = f":{port['port']} {'local' if port['reach'] == 'this-computer' else 'LAN'}"
            x += chip(p, x, cy, label, "acc", "in") + 6
        p.setFont(th.ui_font(11.5))
        p.setPen(QColor(t.muted))
        room = int(r.right() - 10 - x)
        if room > 16:
            p.drawText(QRect(x, cy, room, 22), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(p.font()).elidedText(meta(s), Qt.TextElideMode.ElideRight, room))
        tone_s = "bad" if ph == "failed" else "warn" if ph in ("backoff", "blocked") else "accent"
        paint_spark(p, QRectF(x0, r.bottom() - 22, r.width() - 20, 16), s.get("_spark") or [], th.tone(tone_s))
        p.restore()

def chip(p: QPainter, x: int, y: int, text: str, tone: str, ico: str | None = None, h: int = 22) -> int:
    """Paint a `.chip` at (x, y); returns its width."""
    t = th.current()
    f = th.ui_font(11.5, 500)
    w = QFontMetrics(f).horizontalAdvance(text) + 16 + (17 if ico else 0)
    fg = t.accent_text if tone == "acc" else th.tone(tone) if tone else t.muted
    bg = th.mix(t.accent if tone == "acc" else fg, t.bg, 0.14) if tone else QColor(t.chip)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(x, y, w, h), th.RC, th.RC)
    tx = x + 8
    if ico:
        p.drawPixmap(QPoint(tx, y + (h - 12) // 2), pixmap(ico, fg, 12))
        tx += 17
    p.setPen(QColor(fg))
    p.setFont(f)
    p.drawText(QRectF(tx, y, w, h), Qt.AlignmentFlag.AlignVCenter, text)
    return w

class ServiceList(QListWidget):
    """Grouped by the service's group; filter chips All / Running / Action required / a tag."""

    picked = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ServiceList")
        self.setItemDelegate(ServiceDelegate(self))
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sparks: dict[str, deque[float]] = {}
        self.services: list[dict[str, Any]] = []
        self.filter: Callable[[dict[str, Any]], bool] = lambda s: True
        self.text = ""
        self.order: list[str] = list(QSettings().value("services/order", []) or [])  # your order, from Move up/down
        self.currentItemChanged.connect(self._changed)

    def keyPressEvent(self, e: Any) -> None:
        if e.modifiers() & Qt.KeyboardModifier.AltModifier and e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            item = self.currentItem()
            self.move(item.data(ROLE_NAME) if item else "", -1 if e.key() == Qt.Key.Key_Up else 1)
        else:
            super().keyPressEvent(e)

    def _rank(self, name: str) -> tuple[int, str]:
        return (self.order.index(name) if name in self.order else len(self.order), name)

    def move(self, name: str, step: int) -> None:
        """Move a service up or down within its group (Alt+Up / Alt+Down); the order is kept in your settings."""
        names = sorted((s["spec"]["name"] for s in self.services), key=self._rank)
        group = {s["spec"]["name"]: s["spec"].get("group") or "" for s in self.services}
        mates = [n for n in names if group.get(n) == group.get(name)]
        if name not in mates or not 0 <= (j := mates.index(name) + step) < len(mates):
            return
        a, b = names.index(name), names.index(mates[j])
        names[a], names[b] = names[b], names[a]
        self.order = names
        QSettings().setValue("services/order", names)
        self.update_services(self.services)

    def _changed(self, cur: QListWidgetItem | None, _old: QListWidgetItem | None) -> None:
        if cur and cur.data(ROLE_NAME):
            self.picked.emit(cur.data(ROLE_NAME))

    def set_filter(self, fn: Callable[[dict[str, Any]], bool]) -> None:
        self.filter = fn
        self.update_services(self.services)

    def set_text(self, text: str) -> None:
        self.text = text.lower().strip()
        self.update_services(self.services)

    def select(self, name: str) -> None:
        for i in range(self.count()):
            if self.item(i).data(ROLE_NAME) == name and self.currentRow() != i:
                self.blockSignals(True)
                self.setCurrentRow(i)
                self.blockSignals(False)
        self.viewport().update()

    def update_services(self, services: list[dict[str, Any]]) -> None:
        self.services = services
        shown = [s for s in services if self.filter(s) and (not self.text or self.text in s["spec"]["name"])]
        groups: dict[str, list[dict[str, Any]]] = {}
        for s in shown:
            groups.setdefault(s["spec"].get("group") or "", []).append(s)
        rows: list[tuple[str, dict[str, Any] | None]] = []
        named = len(groups) > 1 or "" not in groups
        for g in sorted(groups, key=lambda g: (g == "", g.lower())):
            if named:
                rows.append((g or "Other", None))
            rows += [("", s) for s in sorted(groups[g], key=lambda s: self._rank(s["spec"]["name"]))]
        keys = [(g, s["spec"]["name"] if s else "") for g, s in rows]
        current = self.currentItem().data(ROLE_NAME) if self.currentItem() else None
        if [(self.item(i).data(ROLE_GROUP) or "", self.item(i).data(ROLE_NAME) or "") for i in range(self.count())] != keys:
            self.blockSignals(True)
            self.clear()
            for g, s in rows:
                it = QListWidgetItem()
                if s is None:
                    it.setData(ROLE_GROUP, g)
                    it.setFlags(Qt.ItemFlag.NoItemFlags)
                else:
                    it.setData(ROLE_NAME, s["spec"]["name"])
                self.addItem(it)
            self.blockSignals(False)
        for i, (_g, s) in enumerate(rows):
            if s is not None:
                s["_spark"] = list(self.sparks.get(s["spec"]["name"], ()))
                self.item(i).setData(ROLE_SVC, s)
        if current:
            self.select(current)
        self.viewport().update()

    def add_sample(self, name: str, cpu: float) -> None:
        self.sparks.setdefault(name, deque(maxlen=48)).append(cpu)

# --- security stack -------------------------------------------------------------------------------------------------
class JailStack(QWidget):
    """Nested dashed boxes, outermost first, ending in the program: what really wraps this service, from its plan."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str, str]] = []  # (left, right, status)
        self.command = ""
        self.refused: dict[str, Any] | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, rows: list[tuple[str, str, str]], command: str, refused: dict[str, Any] | None) -> None:
        self.rows, self.command, self.refused = rows, command, refused
        self._fit()
        self.update()

    def _fit(self) -> None:
        if self.refused:
            self.setFixedHeight(84)
            return
        fm = QFontMetrics(th.mono_font(11))
        w, h = self.width() - 16, 38
        for left, right, _st in self.rows:
            h += 46 if fm.horizontalAdvance(left) + fm.horizontalAdvance(right) + 12 > w else 31
            w -= 16
        self.setFixedHeight(h)

    def resizeEvent(self, e: object) -> None:
        self._fit()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mono = th.mono_font(11)
        p.setFont(mono)
        fm = QFontMetrics(mono)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.refused:
            p.setPen(QPen(QColor(t.bad), 1, Qt.PenStyle.DashLine))
            p.setBrush(th.mix(t.bad, t.raised, 0.06))
            p.drawRoundedRect(r, th.RC, th.RC)
            p.setPen(QColor(t.bad))
            p.drawText(r.adjusted(10, 8, -10, -8), Qt.TextFlag.TextWordWrap,
                       f"Refused · {self.refused['code']}\n{self.refused['message']}")
            return
        for left, right, status in self.rows:
            col = th.tone(LAYER_TONE.get(status, "ok"))
            pen = QPen(th.mix(col, t.raised, 0.45), 1, Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3, 3])
            p.setPen(pen)
            p.setBrush(th.mix(col, t.raised, 0.05))
            p.drawRoundedRect(r, th.RC, th.RC)
            line = QRectF(r.left() + 8, r.top() + 5, r.width() - 16, 18)
            two = fm.horizontalAdvance(left) + fm.horizontalAdvance(right) + 12 > line.width()
            p.setPen(QColor(col))
            p.drawText(line, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(left, Qt.TextElideMode.ElideRight, int(line.width())))
            p.setPen(QColor(t.muted))
            p.drawText(line.translated(0, 15) if two else line, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(right, Qt.TextElideMode.ElideRight, int(line.width())))
            r = r.adjusted(8, 41 if two else 26, -8, -5)
        prog = QRectF(r.left(), r.top() + 1, r.width(), 32)
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(QColor(t.console))
        p.drawRoundedRect(prog, 5, 5)
        p.setFont(th.mono_font(12, 600))
        p.setPen(QColor(t.console_text))
        p.drawText(prog, Qt.AlignmentFlag.AlignCenter,
                   QFontMetrics(p.font()).elidedText(self.command, Qt.TextElideMode.ElideMiddle, int(prog.width() - 16)))

def stack_rows(s: dict[str, Any], host_os: str) -> list[tuple[str, str, str]]:
    """The security stack's rows from a service's plan: limits outermost, then network, the tree, files and syscalls."""
    layers = {x["key"]: x for x in (s.get("plan") or {}).get("layers", [])}
    spec = s["spec"]
    lim = spec["limits"]
    out: list[tuple[str, str, str]] = []

    def short(by: str) -> str:
        by = by.split(" (")[0].split(";")[0]
        for long, name in (("own network namespace", "own netns"), ("own namespace", "own netns"), ("network namespace", "own netns"),
                           ("AppContainer capabilities", "AppContainer"), ("Seatbelt profile", "Seatbelt")):
            if by.startswith(long):
                return name
        return by.split(" + ")[0]

    if "limits" in layers:
        x = layers["limits"]
        out.append((short(x["by"]) or "limits", f"{lim['memory_mb']} MB · {lim['cpu']:g} CPU · {lim['tasks']}", x["status"]))
    if "network" in layers:
        x = layers["network"]
        traffic = spec["network"]["traffic"]["mode"]
        right = {"off": "no route out", "hosts": "gate · loopback off", "any": "internet · LAN off"}.get(traffic, "")
        out.append((short(x["by"]) or "network", right, x["status"]))
    if "tree" in layers:
        x = layers["tree"]
        name = "bubblewrap" if host_os == "linux" and spec["mode"] == "sandbox" else short(x["by"])
        out.append((name, "pid ipc uts user" if host_os == "linux" else "own process tree", x["status"]))
    inner = [layers[k] for k in ("files", "syscalls") if k in layers and layers[k]["status"] != "off"]
    if inner:
        names = " + ".join(dict.fromkeys(short(x["by"]).split(" + ")[-1].lower() for x in inner))
        if host_os == "linux" and spec["mode"] == "sandbox":
            names = "landlock + seccomp"
        worst = "not-enforced" if any(x["status"] == "not-enforced" for x in inner) else (
            "reduced" if any(x["status"] == "reduced" for x in inner) else inner[0]["status"])
        out.append((names, " · ".join(x["title"].lower() for x in inner), worst))
    return [r for r in out if r[2] != "off"]
