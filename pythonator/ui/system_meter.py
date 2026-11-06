"""The toolbar's CPU and RAM meter for the whole computer, sampled once a second (two cheap psutil calls).

The brighter part of each bar is Pythonator's own share: this window, the background service, its plugins and
every service it runs, as the background service measures them every two seconds anyway (``set_own``)."""

from __future__ import annotations

import contextlib

import psutil
from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import theme

GB = 1024**3
WARN_AT, CRITICAL_AT = 75.0, 90.0  # percent: the bar turns yellow, then red
_BAR_W, _BAR_H, _GAP, _ITEM_GAP, _PAD = 72, 6, 7, 20, 12


def level_color(percent: float) -> str:
    return theme.color("error" if percent >= CRITICAL_AT else "warn" if percent >= WARN_AT else "accent")


def gb(n: float) -> str:
    return f"{n / GB:.0f}" if n >= 100 * GB else f"{n / GB:.1f}"


class SystemMeter(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.cpu: float | None = None
        self.used = self.total = 0
        self.peak_cpu = self.peak_mem = 0.0
        self.own_cpu = 0.0  # percent of all cores, like ``cpu``
        self.own_mem = 0
        self._service_cpu, self._service_mem = 0.0, 0  # from the background service (percent of one core, bytes)
        self._window = psutil.Process()
        with contextlib.suppress(psutil.Error):
            self._window.cpu_percent(None)
        self._label = QFont(self.font())
        self._label.setPointSizeF(max(6.0, self._label.pointSizeF() * 0.82))
        self._label.setBold(True)
        self._label.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        self._value = theme.mono_font(9)
        psutil.cpu_percent(None)  # the first call only starts the measurement
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sample)
        self._timer.start(1000)

    def sample(self) -> None:
        try:
            cpu, mem = psutil.cpu_percent(None), psutil.virtual_memory()
        except (OSError, psutil.Error):
            return
        self.cpu, self.used, total = cpu, int(mem.total - mem.available), int(mem.total)
        cores = psutil.cpu_count() or 1
        window_cpu, window_mem = 0.0, 0
        with contextlib.suppress(psutil.Error, OSError), self._window.oneshot():
            window_cpu, window_mem = self._window.cpu_percent(None), self._window.memory_info().rss
        self.own_cpu = min(cpu, (self._service_cpu + window_cpu) / cores)
        self.own_mem = min(self.used, self._service_mem + window_mem)
        self.peak_cpu, self.peak_mem = max(self.peak_cpu, cpu), max(self.peak_mem, self.used)
        if total != self.total:
            self.total = total
            self.updateGeometry()
        theme.set_tooltip(self, "This computer, all programs (the brighter part: Pythonator and its services)\n"
                          f"CPU {cpu:.0f}% of {cores} logical cores · peak {self.peak_cpu:.0f}% · "
                          f"Pythonator {self.own_cpu:.1f}%\n"
                          f"RAM {gb(self.used)} of {gb(total)} GB in use · peak {gb(self.peak_mem)} GB · "
                          f"Pythonator {self.own_mem / 1024**2:.0f} MB")
        self.update()

    def set_own(self, cpu: float, memory: int) -> None:
        """Pythonator's background service, plugins and services: CPU in percent of one core, memory in bytes."""
        self._service_cpu, self._service_mem = max(0.0, cpu), max(0, memory)

    def _widths(self) -> tuple[int, int, int, int]:
        lf, vf = QFontMetrics(self._label), QFontMetrics(self._value)
        label = max(lf.horizontalAdvance("CPU"), lf.horizontalAdvance("RAM"))
        cpu = vf.horizontalAdvance("100%")
        top = gb(self.total) if self.total else "99.9"
        ram = vf.horizontalAdvance(f"{top} / {top} GB")  # room for the widest value: the meter never jiggles
        item = label + _GAP + _BAR_W + _GAP
        return label, cpu, ram, item * 2 + cpu + _ITEM_GAP + ram + 2 * _PAD

    def sizeHint(self) -> QSize:
        height = max(QFontMetrics(self._value).height(), QFontMetrics(self._label).height())
        return QSize(self._widths()[3], height + 12)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event: QPaintEvent | None) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        label_w, cpu_w, ram_w, _ = self._widths()
        h = self.height()
        p.setPen(QColor(theme.color("edge")))
        p.setBrush(QColor(theme.color("bg")))
        p.drawRoundedRect(QRectF(0.5, 2.5, self.width() - 1, h - 5), theme.R_MD, theme.R_MD)
        ram = self.used / self.total * 100 if self.total else None
        own_ram = self.own_mem / self.total * 100 if self.total else 0.0
        items = [("CPU", self.cpu, self.own_cpu, "—" if self.cpu is None else f"{self.cpu:.0f}%", cpu_w),
                 ("RAM", ram, own_ram, f"{gb(self.used)} / {gb(self.total)} GB" if self.total else "—", ram_w)]
        x = float(_PAD)
        for label, percent, own, value, value_w in items:
            p.setFont(self._label)
            p.setPen(QColor(theme.color("muted")))
            p.drawText(QRectF(x, 0, label_w, h), Qt.AlignmentFlag.AlignVCenter, label)
            x += label_w + _GAP
            track = QRectF(x, (h - _BAR_H) / 2, _BAR_W, _BAR_H)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.color("edge")))
            p.drawRoundedRect(track, 1, 1)
            if percent:
                fill = QRectF(track)
                fill.setWidth(max(_BAR_H, track.width() * min(percent, 100.0) / 100))
                color = QColor(level_color(percent))
                p.setBrush(color.darker(165))  # the other programs: a darker shade
                p.drawRoundedRect(fill, 1, 1)
                if own > 0:  # Pythonator's share, from the left, in the full colour
                    mine = QRectF(track)
                    mine.setWidth(min(fill.width(), max(2.0, track.width() * min(own, 100.0) / 100)))
                    p.setBrush(color)
                    p.drawRoundedRect(mine, 1, 1)
            x += _BAR_W + _GAP
            p.setFont(self._value)
            hot = percent is not None and percent >= WARN_AT
            p.setPen(QColor(level_color(percent) if hot and percent is not None else theme.color("text")))
            p.drawText(QRectF(x, 0, value_w, h), Qt.AlignmentFlag.AlignVCenter, value)
            x += value_w + _ITEM_GAP
        p.end()
