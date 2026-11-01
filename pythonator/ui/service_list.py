"""Sidebar list of services with live status, uptime and resource usage."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from ..core.models import LaunchMode, Runtime
from ..core.runtimes import runtime_info
from ..supervisor import Service, Status, Supervisor, format_next_run, format_uptime
from . import theme

ServiceRole = Qt.ItemDataRole.UserRole + 1
_Index = QModelIndex | QPersistentModelIndex


def describe_target(svc: Service) -> str:
    cfg = svc.config
    if cfg.mode is LaunchMode.SCRIPT:
        return cfg.target.replace("\\", "/").rsplit("/", 1)[-1] or "no script"
    if cfg.mode is LaunchMode.MODULE:
        if not cfg.target:
            return "no entry point"
        prefix = _MODULE_PREFIX.get(cfg.runtime, "")
        return f"{prefix}{cfg.target}"
    return cfg.target or "no command"


_MODULE_PREFIX = {
    Runtime.PYTHON: "-m ",
    Runtime.NODE: "npm run ",
    Runtime.BUN: "bun run ",
    Runtime.DENO: "deno task ",
    Runtime.GO: "go run ",
    Runtime.JAVA: "java ",
    Runtime.RUBY: "bundle exec ",
    Runtime.PHP: "php -S ",
}


class ServiceListModel(QAbstractListModel):
    def __init__(self, supervisor: Supervisor) -> None:
        super().__init__()
        self._sup = supervisor
        self._ids: list[str] = [s.id for s in supervisor.services()]

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008 - Qt signature
        return 0 if parent.isValid() else len(self._ids)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._ids):
            return None
        svc = self._sup.get(self._ids[index.row()])
        if svc is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return svc.name
        if role == ServiceRole:
            return svc
        if role == Qt.ItemDataRole.ToolTipRole:
            cfg = svc.config
            return f"{svc.name}\n{runtime_info(cfg.runtime).mode_label(cfg.mode)}: {cfg.target or '—'}"
        return None

    def reload(self) -> None:
        self.beginResetModel()
        self._ids = [s.id for s in self._sup.services()]
        self.endResetModel()

    def row_of(self, service_id: str) -> int:
        try:
            return self._ids.index(service_id)
        except ValueError:
            return -1

    def id_at(self, row: int) -> str | None:
        return self._ids[row] if 0 <= row < len(self._ids) else None

    def refresh(self, service_id: str | None = None) -> None:
        if not self._ids:
            return
        if service_id is None:
            self.dataChanged.emit(self.index(0), self.index(len(self._ids) - 1))
        elif (row := self.row_of(service_id)) >= 0:
            self.dataChanged.emit(self.index(row), self.index(row))


class ServiceDelegate(QStyledItemDelegate):
    HEIGHT = 50

    def __init__(self, supervisor: Supervisor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sup = supervisor

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        return QSize(200, self.HEIGHT)

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: _Index) -> None:
        svc = index.data(ServiceRole)
        if painter is None or not isinstance(svc, Service):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect: QRect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        card = rect.adjusted(6, 3, -6, -3)
        if selected:
            painter.setBrush(QColor("#2a3444"))
            painter.setPen(QColor("#3b4d6b"))
            painter.drawRoundedRect(card, 7, 7)
        elif hovered:
            painter.setBrush(QColor(theme.SURFACE))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(card, 7, 7)

        color = QColor(theme.STATUS_COLORS[svc.status])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        cy = card.top() + 17
        painter.drawEllipse(card.left() + 11, cy - 4, 9, 9)

        left = card.left() + 30
        right = card.right() - 10
        base: QFont = option.font
        name_font = QFont(base)
        name_font.setWeight(QFont.Weight.DemiBold)
        small = QFont(base)
        small.setPointSizeF(max(7.0, base.pointSizeF() - 1))

        stats = self._sup.stats_for(svc)
        right_text = f"{stats.cpu:.0f}% · {stats.rss_mb:.0f} MB" if stats else ""
        painter.setFont(small)
        rw = painter.fontMetrics().horizontalAdvance(right_text) if right_text else 0
        if right_text:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(QRect(right - rw, card.top() + 7, rw, 18), Qt.AlignmentFlag.AlignVCenter, right_text)

        painter.setFont(name_font)
        painter.setPen(QColor(theme.TEXT))
        name = painter.fontMetrics().elidedText(svc.name, Qt.TextElideMode.ElideRight, right - left - rw - 8)
        painter.drawText(QRect(left, card.top() + 7, right - left, 18), Qt.AlignmentFlag.AlignVCenter, name)

        painter.setFont(small)
        status = theme.STATUS_LABELS[svc.status]
        if svc.status in (Status.RUNNING, Status.STOPPING):
            status += f" · {format_uptime(svc.uptime)}"
        elif svc.last_exit is not None and svc.status in (Status.FAILED, Status.EXITED):
            status += f" · {svc.last_exit.describe()}"
        if svc.task is not None:
            status += f" · ⚙ {svc.task_label}"
        elif svc.next_run and svc.status not in (Status.RUNNING, Status.STOPPING):
            status += f" · ⏱ {format_next_run(svc.next_run)}"
        sub = f"{status}   {describe_target(svc)}"
        painter.setPen(color if svc.status is Status.FAILED else QColor(theme.TEXT_DIM))
        sub = painter.fontMetrics().elidedText(sub, Qt.TextElideMode.ElideRight, right - left)
        painter.drawText(QRect(left, card.top() + 25, right - left, 16), Qt.AlignmentFlag.AlignVCenter, sub)
        painter.restore()
