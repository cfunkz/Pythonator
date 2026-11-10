"""The service list (groups, filters, rings and live lines) and the inspector's security stack and traffic log."""

from __future__ import annotations

import time
import zlib
from collections import deque
from collections.abc import Callable
from html import escape
from typing import Any

from PyQt6.QtCore import QModelIndex, QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
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
ROLE_SVC, ROLE_NAME = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1

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
def group_color(group: str) -> QColor:
    """A group's own colour, the same on every run and machine (its tag in the filters and the dot on its cards)."""
    return QColor.fromHsl(zlib.crc32(group.encode()) % 360, 150, 150 if th.current() is th.DARK else 95)

class ServiceDelegate(QStyledItemDelegate):
    """A card per service: layer rings · group dot, name, state and status dot · mode and port chips · its CPU line
    along the bottom. Compact (the list header's switch) is a shorter card without the CPU line."""

    SIZE = {False: (76, 12, 14, 20), True: (54, 10, 13, 18)}  # compact → (row height, ring radius, name px, chip h)
    compact = False

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(240, self.SIZE[self.compact][0])

    def paint(self, p: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if p is None:
            return
        t = th.current()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s: dict[str, Any] = index.data(ROLE_SVC) or {}
        spec, ob = s.get("spec", {}), s.get("observed", {})
        _h, ring, name_px, ch = self.SIZE[self.compact]
        r = QRectF(option.rect).adjusted(8, 2, -8, -2)
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        if sel or option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(QPen(QColor(t.border_strong if sel else t.border), 1))
            p.setBrush(QColor(t.raised2 if sel else t.raised))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), th.RC, th.RC)
        x0, y0 = r.left() + 12, r.top() + 8
        paint_rings(p, QPointF(x0 + ring, r.center().y() if self.compact else y0 + 20), layer_states(s), ring)
        tx, ph = int(x0 + 2 * ring + 14), ob.get("phase", "stopped")
        paint_dot(p, QPointF(r.right() - 14, y0 + 10), "off" if ph == "stopped" else PHASE_DOT.get(ph, "idle"))
        if group := spec.get("group"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(group_color(group))
            p.drawEllipse(QPointF(tx + 4, y0 + 10), 4, 4)
            tx += 14
        p.setFont(th.ui_font(11.5))  # the state (up 3h 12m, refused …) sits on the name's line, on the right
        room, words, name = int(r.right() - 28 - tx), meta(s), spec.get("name", "?")
        name_w = QFontMetrics(th.ui_font(name_px, 600)).horizontalAdvance(name)  # the name first, the state gets the rest
        mw = min(QFontMetrics(p.font()).horizontalAdvance(words) + 2, max(56, room - name_w - 16))
        p.setPen(QColor(t.muted))
        p.drawText(QRect(int(r.right()) - 28 - mw, int(y0), mw, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(words, Qt.TextElideMode.ElideRight, mw))
        p.setFont(th.ui_font(name_px, 600))
        p.setPen(QColor(t.text))
        nw = room - mw - 8
        p.drawText(QRect(tx, int(y0), nw, 20), Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(name, Qt.TextElideMode.ElideRight, nw))
        cy = int(y0 + (23 if self.compact else 26))
        text, ico, tone = mode_label(spec)
        x = tx + chip(p, tx, cy, text, tone, ico, ch) + 8
        for port in (spec.get("network") or {}).get("ports", [])[:1]:
            chip(p, x, cy, f":{port['port']} {'local' if port['reach'] == 'this-computer' else 'LAN'}", "acc", "in", ch)
        if not self.compact:
            tone_s = "bad" if ph == "failed" else "warn" if ph in ("backoff", "blocked") else "accent"
            paint_spark(p, QRectF(x0, r.bottom() - 15, r.width() - 24, 11), s.get("_spark") or [], th.tone(tone_s), False, 1.2)
        p.restore()

def chip(p: QPainter, x: int, y: int, text: str, tone: str, ico: str | None = None, h: int = 22) -> int:
    """Paint a `.chip` at (x, y); returns its width."""
    t = th.current()
    f = th.ui_font(11.5 if h >= 22 else 10.5, 500)
    pad = 8 if h >= 22 else 6
    w = QFontMetrics(f).horizontalAdvance(text) + 2 * pad + (17 if ico else 0)
    fg = t.accent_text if tone == "acc" else th.tone(tone) if tone else t.muted
    p.setPen(QPen(th.mix(fg, t.raised, 0.38), 1))  # as kit.Chip: a hairline in its colour, barely tinted
    p.setBrush(th.mix(fg, t.raised, 0.07))
    p.drawRoundedRect(QRectF(x + 0.5, y + 0.5, w - 1, h - 1), th.RC, th.RC)
    tx = x + pad
    if ico:
        p.drawPixmap(QPoint(tx, y + (h - 12) // 2), pixmap(ico, fg, 12))
        tx += 17
    p.setPen(QColor(fg))
    p.setFont(f)
    p.drawText(QRectF(tx, y, w, h), Qt.AlignmentFlag.AlignVCenter, text)
    return w

class ServiceList(QListWidget):
    """Grouped by the service's group; filter chips All / Running / Action required / a tag. Drag a service, or press
    Alt+Up / Alt+Down, to change its place in its group."""

    picked = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ServiceList")
        self.cards = ServiceDelegate(self)
        self.cards.compact = QSettings().value("services/compact", False) in (True, "true")
        self.setItemDelegate(self.cards)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setAccessibleName("Services")
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

    def dropEvent(self, e: Any) -> None:
        """A drop only says where to: Qt's own move is cancelled, and the list is rebuilt from your order once the drag
        has finished (never while Qt still holds the dragged rows)."""
        src, dst = self.currentItem(), self.itemAt(e.position().toPoint())
        e.setDropAction(Qt.DropAction.IgnoreAction)
        e.accept()
        if src is not None and dst is not None and dst.data(ROLE_NAME):
            a, b = src.data(ROLE_NAME), dst.data(ROLE_NAME)
            QTimer.singleShot(0, lambda: self.move(a, to=b))

    def move(self, name: str, step: int = 0, to: str = "") -> None:
        """Move a service by `step` places, or to where `to` is, within its group; the order is kept in your settings."""
        names = sorted((s["spec"]["name"] for s in self.services), key=self._rank)
        group = {s["spec"]["name"]: s["spec"].get("group") or "" for s in self.services}
        mates = [n for n in names if group.get(n) == group.get(name)]
        if name not in mates or not (to or 0 <= mates.index(name) + step < len(mates)):
            return
        to = to or mates[mates.index(name) + step]
        if to == name or to not in mates:
            return
        down = names.index(to) > names.index(name)
        names.remove(name)
        names.insert(names.index(to) + down, name)
        self.order = names
        QSettings().setValue("services/order", names)
        self.update_services(self.services)

    def set_compact(self, on: bool) -> None:
        """Cards or compact rows, at once; kept in your settings."""
        self.cards.compact = on
        QSettings().setValue("services/compact", on)
        self.doItemsLayout()

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
        rows = sorted((s for s in services if self.filter(s) and (not self.text or self.text in s["spec"]["name"])),
                      key=lambda s: (not s["spec"].get("group"), (s["spec"].get("group") or "").lower(), self._rank(s["spec"]["name"])))
        current = self.currentItem().data(ROLE_NAME) if self.currentItem() else None
        if [self.item(i).data(ROLE_NAME) for i in range(self.count())] != [s["spec"]["name"] for s in rows]:
            self.blockSignals(True)
            self.clear()
            for s in rows:  # grouped by group (a coloured dot on each card), then in your order
                (it := QListWidgetItem()).setData(ROLE_NAME, s["spec"]["name"])
                self.addItem(it)
            self.blockSignals(False)
        for i, s in enumerate(rows):
            s["_spark"] = list(self.sparks.get(s["spec"]["name"], ()))
            group = f" · group {s['spec']['group']}" if s["spec"].get("group") else ""
            it, words = self.item(i), f"{s['spec']['name']}{group} · {mode_label(s['spec'])[0]} · {meta(s)}"
            it.setData(ROLE_SVC, s)
            it.setToolTip(f"<qt>{escape(words)}</qt>")  # plain text: a name or message is never markup
            it.setData(Qt.ItemDataRole.AccessibleTextRole, words)
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
        p.drawRoundedRect(prog, th.RC, th.RC)
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
