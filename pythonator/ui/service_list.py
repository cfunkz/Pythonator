"""The sidebar's services: a compact card each, with a status dot, the name, how it's isolated, live CPU and memory,
and a status line (uptime, the next run, the last exit). Painted by one delegate, so a hundred services and a
repaint every second cost nothing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import (
    QAbstractListModel,
    QElapsedTimer,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from ..core.models import Service
from . import theme

MODE_LABEL = {"sandbox": "Sandbox", "container": "Container", "full": "Full access"}
_Index = QModelIndex | QPersistentModelIndex
RowRole = Qt.ItemDataRole.UserRole + 1


@dataclass
class Row:
    service: Service
    state: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return str(self.state.get("state", "stopped"))

    @property
    def mode(self) -> str:
        return self.service.mode.value


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def when(epoch: float) -> str:
    """A time ahead, as a person says it: "in 4m", "14:30", "tomorrow 07:00", "Mon 07:00"."""
    left = epoch - time.time()
    if left < 60:
        return "now"
    if left < 3600:
        return f"in {int(left // 60)}m"
    at = time.localtime(epoch)
    days = (time.mktime((*at[:3], 0, 0, 0, 0, 0, -1))
            - time.mktime((*time.localtime()[:3], 0, 0, 0, 0, 0, -1))) // 86400
    clock = time.strftime("%H:%M", at)
    return clock if days == 0 else f"tomorrow {clock}" if days == 1 else time.strftime("%a %H:%M", at)


def entry_text(service: Service) -> str:
    return service.entry or " ".join(service.command[:2]) or service.runtime


def status_text(row: Row) -> str:
    st, state = row.state, row.status
    text = theme.STATE_LABELS.get(state, state.capitalize())
    if state in ("running", "stopping") and st.get("started"):
        text += f" {uptime(time.time() - float(st['started']))}"
    elif state == "task" and st.get("task"):
        text += f" · {st['task']}"
    elif state == "failed" and st.get("exit") is not None:
        text += f" · exit {st['exit']}"
    if state not in theme.LIVE and st.get("next_run"):
        text += f" · ⏱ {when(float(st['next_run']))}"
    return text


def tooltip(row: Row) -> str:
    s = row.service
    mode = MODE_LABEL.get(row.mode, row.mode)
    if row.mode != "full":
        mode += ", network on" if s.network.enabled else ", offline"
    if s.network.ports:
        mode += " · serves on " + ", ".join(map(str, s.network.ports))
    lines = [f"{s.name} · {status_text(row)}", f"{entry_text(s)} in {s.folder}", mode]
    if row.state.get("note"):
        lines.append(str(row.state["note"]))
    return "\n".join(lines)


class ServiceModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: list[Row] = []
        self.by_id: dict[str, Row] = {}

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008 - Qt's signature
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == RowRole:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            return row.service.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip(row)
        return None

    def set_services(self, services: list[Service]) -> None:
        old = self.by_id
        self.beginResetModel()
        self.rows = [Row(s, old[s.id].state, old[s.id].stats) if s.id in old else Row(s) for s in services]
        self.by_id = {r.service.id: r for r in self.rows}
        self.endResetModel()

    def changed(self, sid: str | None = None) -> None:
        if not self.rows:
            return
        if sid is None:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))
        elif (row := self.by_id.get(sid)) is not None:
            i = self.index(self.rows.index(row))
            self.dataChanged.emit(i, i)


class HoverFade(QObject):
    """Fades the highlight of the row under the mouse in and out, as native lists do. Only fading rows repaint,
    and the timer runs only while something fades."""

    FADE_IN_MS, FADE_OUT_MS, FRAME_MS = 90, 160, 16

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        self._target: QPersistentModelIndex | None = None
        self._levels: dict[QPersistentModelIndex, float] = {}
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._step)
        if viewport := view.viewport():
            viewport.installEventFilter(self)

    def level(self, index: _Index) -> float:
        return self._levels.get(QPersistentModelIndex(index), 0.0)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if isinstance(event, QMouseEvent) and event.type() == QEvent.Type.MouseMove:
            self._hover(self._view.indexAt(event.position().toPoint()))
        elif event is not None and event.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            self._hover(None)
        return False

    def _hover(self, index: QModelIndex | None) -> None:
        target = QPersistentModelIndex(index) if index is not None and index.isValid() else None
        if target == self._target:
            return
        self._target = target
        if target is not None:
            self._levels.setdefault(target, 0.0)
        if not self._timer.isActive():
            self._clock.start()
            self._timer.start()

    def _step(self) -> None:
        elapsed = max(1, self._clock.restart())
        for index in list(self._levels):
            level = self._levels[index]
            if index == self._target:
                level = min(1.0, level + elapsed / self.FADE_IN_MS)
            else:
                level = max(0.0, level - elapsed / self.FADE_OUT_MS)
            if not index.isValid() or (level == 0.0 and index != self._target):
                del self._levels[index]
            else:
                self._levels[index] = level
            if index.isValid():
                self._view.update(QModelIndex(index))
        if all(v == 1.0 for v in self._levels.values()):
            self._timer.stop()


class ServiceDelegate(QStyledItemDelegate):
    HEIGHT, ICON = 48, 12

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self.fade = HoverFade(view)
        self.mono = theme.mono_font(8)

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(200, self.HEIGHT)

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: _Index) -> None:
        row = index.data(RowRole)
        if painter is None or not isinstance(row, Row):
            return
        c = theme.current
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(option.rect.adjusted(8, 2, -8, -2)).adjusted(0.5, 0.5, -0.5, -0.5)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(c.selection))
            edge = QColor(c.accent)
            edge.setAlpha(110)
            painter.setPen(edge)
            painter.drawRoundedRect(card, theme.R_MD, theme.R_MD)
        elif (level := self.fade.level(index)) > 0:
            hover = QColor(c.raised)
            hover.setAlphaF(level)
            painter.setBrush(hover)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(card, theme.R_MD, theme.R_MD)
        top = round(card.top()) + 7
        theme.draw_state(painter, row.status, QPointF(card.left() + 15, top + 8.5))
        left, right = round(card.left()) + 30, round(card.right()) - 10

        usage = ""
        if row.status in theme.LIVE and row.stats:
            usage = f"{float(row.stats.get('cpu', 0)):.0f}%  {human_bytes(float(row.stats.get('memory', 0)))}"
        painter.setFont(self.mono)
        uw = painter.fontMetrics().horizontalAdvance(usage) if usage else 0
        if usage:
            painter.setPen(QColor(c.muted))
            painter.drawText(QRect(right - uw, top, uw, 17), Qt.AlignmentFlag.AlignVCenter, usage)

        bold = QFont(option.font)
        bold.setWeight(QFont.Weight.DemiBold)
        painter.setFont(bold)
        painter.setPen(QColor(c.text))
        room = right - left - uw - 8 - (self.ICON + 6)
        name = painter.fontMetrics().elidedText(row.service.name, Qt.TextElideMode.ElideRight, max(20, room))
        painter.drawText(QRect(left, top, right - left, 17), Qt.AlignmentFlag.AlignVCenter, name)
        if room >= 20:
            x = left + painter.fontMetrics().horizontalAdvance(name) + 6
            theme.draw_mode(painter, row.mode, QRectF(x, top + 2.5, self.ICON, self.ICON),
                            offline=row.mode != "full" and not row.service.network.enabled)

        painter.setFont(self.mono)
        fm = painter.fontMetrics()
        status = fm.elidedText(status_text(row), Qt.TextElideMode.ElideRight, right - left)
        alarm = row.status in ("failed", "waiting")
        painter.setPen(QColor(theme.state_color(row.status) if alarm else c.muted))
        painter.drawText(QRect(left, top + 18, right - left, 15), Qt.AlignmentFlag.AlignVCenter, status)
        sw = fm.horizontalAdvance(status + "  ")
        if sw < right - left:
            painter.setPen(QColor(c.faint))
            target = fm.elidedText(entry_text(row.service), Qt.TextElideMode.ElideRight, right - left - sw)
            painter.drawText(QRect(left + sw, top + 18, right - left - sw, 15), Qt.AlignmentFlag.AlignVCenter, target)
        painter.restore()


class ServiceList(QListView):
    selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ServiceList")
        self.rows = ServiceModel(self)
        self.setModel(self.rows)
        self.setItemDelegate(ServiceDelegate(self))
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        if sel := self.selectionModel():
            sel.currentChanged.connect(lambda cur, _prev: self.selected.emit(self.id_at(cur)))
        self._clock = QTimer(self)  # uptimes and "in 4m" move on once a second
        self._clock.timeout.connect(lambda: self.rows.changed())
        self._clock.start(1000)

    def id_at(self, index: QModelIndex) -> str:
        row = index.data(RowRole) if index.isValid() else None
        return row.service.id if isinstance(row, Row) else ""

    def current_id(self) -> str:
        return self.id_at(self.currentIndex())

    def select(self, sid: str) -> None:
        row = self.rows.by_id.get(sid)
        if row is not None:
            self.setCurrentIndex(self.rows.index(self.rows.rows.index(row)))

    def set_services(self, services: list[Service]) -> None:
        keep = self.current_id()
        self.rows.set_services(services)
        if keep in self.rows.by_id:
            self.select(keep)
        elif self.rows.rows:
            self.setCurrentIndex(self.rows.index(0))
        else:
            self.selected.emit("")

    def set_state(self, st: dict[str, Any]) -> None:
        row = self.rows.by_id.get(str(st.get("id")))
        if row is not None:
            row.state = st
            if row.status not in theme.LIVE:
                row.stats = {}
            self.rows.changed(row.service.id)

    def set_stats(self, items: dict[str, dict[str, Any]]) -> None:
        for sid, stats in items.items():
            if (row := self.rows.by_id.get(sid)) is not None:
                row.stats = stats
        self.rows.changed()

    def row(self, sid: str) -> Row | None:
        return self.rows.by_id.get(sid)
