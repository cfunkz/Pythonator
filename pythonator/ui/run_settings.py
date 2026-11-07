"""The service settings' Limits and Schedule & restarts tabs.

Limits: one card each for memory and CPU, with a slider over this computer's own range, one-click sizes and a box
to type any value (512M, 2G, 1.5 cores, 150%), checked as you type. Profiles at the top set both at once.

Schedule & restarts: keep it running (restarted by the rules) or run it on a schedule, picked from presets or a
small form (every day at 09:00, Monday to Friday, the 1st of the month…) and shown with its next runs. The schedule
is still saved as text, "every 15m" or a cron line, which can be typed too.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from PyQt6.QtCore import Qt, QTime, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..core import limits, schedule
from ..paths import IS_WINDOWS
from . import theme
from .widgets import dspin, label, line, spin

MEMORY, CPU = "memory", "cpu"
# Slider stops: sizes people pick, up to (not including) all of this computer, which is the last stop: no limit.
_MEMORY_STOPS = (64, 128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768,
                 49152, 65536, 98304, 131072, 196608, 262144, 393216, 524288, 786432)
_CPU_STOPS = (10, 25, 50, 75, 100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2400, 3200, 4800, 6400, 9600, 12800)


def _muted(text: str) -> QLabel:
    return label(text, muted=True)


def _title(text: str) -> QLabel:
    w = label(text, wrap=False)
    w.setStyleSheet(f"font-size: {11 * theme.POINT_SCALE:g}pt; font-weight: 700;")
    return w


def card(name: str) -> QFrame:
    """A panel-coloured box with a hairline, for one group of settings."""
    box = QFrame()
    box.setObjectName(name)
    box.setStyleSheet(f"#{name} {{ background: {theme.color('panel')}; border: 1px solid {theme.color('border')}; "
                      f"border-radius: {theme.R_LG}px; }} #{name} QLabel {{ background: none; }}")
    return box


class Segmented(QWidget):
    """A row of buttons, one of them pressed: a compact radio group. ``changed`` sends the new key."""

    changed = pyqtSignal(str)

    def __init__(self, options: Sequence[tuple[str, str]], value: str, *, tall: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in options:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setProperty("segment", True)
            if tall:
                b.setMinimumHeight(52)
                b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group.addButton(b)
            self.buttons[key] = b
            row.addWidget(b, 1 if tall else 0)
            b.toggled.connect(lambda on, k=key: self._toggled(on, k))
        if not tall:
            row.addStretch(1)
        self.set_value(value)

    def _toggled(self, on: bool, key: str) -> None:
        if on:
            self.changed.emit(key)

    def value(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), next(iter(self.buttons)))

    def set_value(self, key: str) -> None:
        (self.buttons.get(key) or next(iter(self.buttons.values()))).setChecked(True)


# -- limits ------------------------------------------------------------------------------------------------------


class LimitCard(QFrame):
    """One limit: its value large on the right, a slider over this computer's range, sizes to click and a box."""

    edited = pyqtSignal()

    def __init__(self, kind: str, start: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.start = start
        self.n, self.total = limits.cores(), limits.memory_total_mb()
        memory = kind == MEMORY
        self.stops = [s for s in (_MEMORY_STOPS if memory else _CPU_STOPS)
                      if s < (self.total if memory else self.n * 100)]
        self.setObjectName(f"Limit{kind.title()}")
        self.setStyleSheet(f"#{self.objectName()} {{ background: {theme.color('panel')}; "
                           f"border: 1px solid {theme.color('border')}; border-radius: {theme.R_LG}px; }} "
                           f"#{self.objectName()} QLabel {{ background: none; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(_title("Memory" if memory else "CPU"))
        left.addWidget(_muted("The most memory the service and all its child processes may hold together."
                              if memory else
                              "The most CPU the service and all its child processes may use together."))
        head.addLayout(left, 1)
        right = QVBoxLayout()
        right.setSpacing(0)
        self.big = label("", wrap=False)
        self.big.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.share = _muted("")
        self.share.setWordWrap(False)
        self.share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.addWidget(self.big)
        right.addWidget(self.share)
        head.addSpacing(12)
        head.addLayout(right)
        layout.addLayout(head)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(self.stops))  # the last stop: no limit
        self.slider.setPageStep(1)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setToolTip("Drag for more or less. All the way right: no limit.")
        self.slider.valueChanged.connect(self._slid)
        layout.addWidget(self.slider)
        scale = QHBoxLayout()
        lo = self._fmt(self.stops[0]) if self.stops else ""
        scale.addWidget(_muted(lo))
        scale.addStretch(1)
        scale.addWidget(_muted(f"No limit (all {self.total / 1024:.1f} GB)" if memory else
                               f"No limit (all {self.n} cores)"))
        layout.addLayout(scale)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.chips: list[tuple[QPushButton, str]] = []
        for text, value in self._presets():
            chip = QPushButton(text)
            chip.setCheckable(True)
            chip.setAutoDefault(False)
            chip.setProperty("segment", True)
            chip.setToolTip(f"Set it to {value}" if value else "No limit")
            chip.clicked.connect(lambda _c=False, v=value: self.edit.setText(v))
            self.chips.append((chip, value))
            row.addWidget(chip)
        row.addStretch(1)
        row.addWidget(_muted("or"))
        self.edit = line(limits.format_memory(start) if memory else limits.format_cpu(start),
                         "e.g. 512M, 2G" if memory else "e.g. 1.5, 150%")
        self.edit.setFixedWidth(150)
        self.edit.textChanged.connect(self.refresh)
        row.addWidget(self.edit)
        layout.addLayout(row)
        self.note = _muted("")
        layout.addWidget(self.note)
        self._sliding = False
        self._unlimited_style: bool | None = None
        self.refresh()

    def _presets(self) -> list[tuple[str, str]]:
        if self.kind == MEMORY:
            quarter = limits.format_memory(max(limits.MIN_MEMORY_MB, self.total // 4 // 512 * 512))
            return [("No limit", ""), ("256 MB", "256 MB"), ("512 MB", "512 MB"), ("1 GB", "1 GB"),
                    ("2 GB", "2 GB"), ("A quarter", quarter)]
        out = [("No limit", ""), ("¼ core", "0.25 cores"), ("½ core", "0.5 cores"), ("1 core", "1 core")]
        if self.n > 2:
            out.append(("2 cores", "2 cores"))
        out.append(("Half", limits.format_cpu(self.n * 50) if self.n > 1 else "0.5 cores"))
        return out

    def _fmt(self, value: int) -> str:
        return limits.format_memory(value) if self.kind == MEMORY else limits.format_cpu(value)

    def _parse(self) -> tuple[int, str]:
        if self.kind == MEMORY:
            return limits.parse_memory(self.edit.text())
        return limits.parse_cpu(self.edit.text())

    def value(self) -> int:
        """The limit (MB, or percent of one core). Raises ValueError with what to write instead. Unchanged text
        keeps the saved value, even one set on a bigger computer."""
        if self.edit.text() == self._fmt(self.start):
            return self.start
        return self._parse()[0]

    def _slid(self, pos: int) -> None:
        if self._sliding:
            return
        self.edit.setText("" if pos >= len(self.stops) else self._fmt(self.stops[pos]))

    def _position(self, value: int) -> int:
        if not value or not self.stops:
            return len(self.stops)
        return min(range(len(self.stops)), key=lambda i: abs(math.log(self.stops[i]) - math.log(value)))

    def _style_slider(self, unlimited: bool) -> None:
        if unlimited == self._unlimited_style:
            return
        self._unlimited_style = unlimited
        fill = theme.rgba("accent", 0.28) if unlimited else theme.color("accent")
        knob = theme.color("muted") if unlimited else theme.color("accent_hi")
        self.slider.setStyleSheet(
            f"QSlider {{ background: none; min-height: 22px; }}"
            f"QSlider::groove:horizontal {{ height: 6px; background: {theme.color('edge')}; border-radius: 3px; }}"
            f"QSlider::sub-page:horizontal {{ background: {fill}; border-radius: 3px; }}"
            f"QSlider::handle:horizontal {{ background: {knob}; border: 2px solid {theme.color('panel')}; "
            f"width: 14px; height: 14px; margin: -6px 0; border-radius: 9px; }}")

    def refresh(self) -> None:
        memory = self.kind == MEMORY
        try:
            value, why = (self.start, "") if self.edit.text() == self._fmt(self.start) else self._parse()
        except limits.LimitError as exc:
            why = str(exc).split(": ", 1)[1]
            self._show("Not a size" if memory else "Not a number", "", why[:1].upper() + why[1:], "error")
            self.edited.emit()
            return
        if value:
            share = value * 100 / self.total if memory else value / self.n
            self.big.setText(self._fmt(value))
            self.share.setText(f"{share:.0f}% of this computer's {'memory' if memory else 'CPU'}")
            over = ("Windows refuses it memory past that" if IS_WINDOWS else
                    "going over stops it, then its restart rule applies")
            text = (f"Held to {limits.format_memory(value)}: {over}." if memory else
                    f"Held to {limits.describe(value)}: slowed down when it wants more, never stopped.")
        else:
            self.big.setText("No limit")
            self.share.setText("all of this computer")
            text = "No limit: it may use everything this computer has."
        self._show(self.big.text(), self.share.text(), text + (f" ({why})" if why else ""),
                   "warn" if why else "ok" if value else "muted", value)
        self.edited.emit()

    def _show(self, big: str, share: str, note: str, tone: str, value: int | None = None) -> None:
        colour = theme.color("accent_hi" if value else "error" if tone == "error" else "muted")
        self.big.setText(big)
        self.big.setStyleSheet(f"font-size: {17 * theme.POINT_SCALE:g}pt; font-weight: 700; color: {colour};")
        self.share.setText(share)
        self.note.setText(note)
        self.note.setStyleSheet(f"color: {theme.color(tone)}")
        self.edit.setStyleSheet(f"border-color: {theme.color('error')}" if tone == "error" else "")
        if value is not None:
            self._sliding = True
            self.slider.setValue(self._position(value))
            self._sliding = False
            self._style_slider(not value)
        text = self.edit.text().strip()
        for chip, v in self.chips:
            chip.setChecked(value is not None and (v == text or (not v and not value)))


class LimitsTab(QWidget):
    """Memory and CPU, each in its own card, with profiles that set both."""

    PROFILES = (("Small bot", "256 MB", "0.25 cores"), ("Web app", "1 GB", "1 core"),
                ("Worker", "2 GB", "2 cores"), ("No limits", "", ""))

    def __init__(self, d: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        n, total = limits.cores(), limits.memory_total_mb()
        top = QHBoxLayout()
        hi = theme.color("accent_hi")
        computer = QLabel(f"This computer &nbsp;<b style='color:{hi}'>{n} cores</b> &nbsp;·&nbsp; "
                          f"<b style='color:{hi}'>{total / 1024:.1f} GB memory</b>")
        computer.setTextFormat(Qt.TextFormat.RichText)  # only numbers go in it
        top.addWidget(computer)
        top.addStretch(1)
        top.addWidget(_muted("Start from"))
        self.memory = LimitCard(MEMORY, int(d.get("memory_mb", 0)))
        self.cpu = LimitCard(CPU, int(d.get("cpu_percent", 0)))
        self.profiles: list[QPushButton] = []
        for name, mem, cpu in self.PROFILES:
            b = QPushButton(name)
            b.setAutoDefault(False)
            b.setProperty("segment", True)
            b.setToolTip(f"{mem or 'No limit'} memory, {cpu or 'no limit'} CPU")
            b.clicked.connect(lambda _c=False, m=mem, c=cpu: self.set_both(m, c))
            self.profiles.append(b)
            top.addWidget(b)
        layout.addLayout(top)
        layout.addWidget(self.memory)
        layout.addWidget(self.cpu)
        how = ("Windows holds the tree to it with a job object" if IS_WINDOWS else
               "Pythonator pauses the tree in short slices whenever it goes over its CPU")
        layout.addWidget(_muted(f"Limits cover the whole process tree: every thread and child process together. "
                                f"1 core is 100%. {how}; containers use --cpus and --memory. A saved limit "
                                "applies from the next start."))
        layout.addStretch(1)

    def set_both(self, memory: str, cpu: str) -> None:
        self.memory.edit.setText(memory)
        self.cpu.edit.setText(cpu)

    def values(self) -> tuple[int, int]:
        """(memory_mb, cpu_percent). Raises ValueError with what to write instead."""
        return self.memory.value(), self.cpu.value()


# -- schedule & restarts -----------------------------------------------------------------------------------------

KEEP, SCHEDULED = "keep", "scheduled"
_RESTART_NOTES = {
    "on-failure": "Restarted after a crash (a non-zero exit). A clean exit stays stopped.",
    "always": "Restarted whenever it exits, after a clean exit too.",
    "never": "Left stopped when it exits, until you start it.",
}


class ScheduleTab(QWidget):
    """When the service runs: kept running (with restart rules) or on a schedule (presets, a form, or text)."""

    def __init__(self, d: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        text = str(d.get("schedule", "") or "")
        self._syncing = False
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.run_mode = Segmented(((KEEP, "Keep it running\nA server, a bot, a worker"),
                                   (SCHEDULED, "Run on a schedule\nA job: backups, reports, scrapers")),
                                  SCHEDULED if text else KEEP, tall=True)
        layout.addWidget(self.run_mode)
        self.autostart = QCheckBox("Start when Pythonator starts")
        self.autostart.setChecked(bool(d.get("autostart")))
        layout.addWidget(self.autostart)

        # the schedule
        self.when_box = card("ScheduleCard")
        wl = QVBoxLayout(self.when_box)
        wl.setContentsMargins(14, 12, 14, 12)
        wl.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(_title("Schedule"))
        head.addStretch(1)
        self.summary = label("", wrap=False)
        head.addWidget(self.summary)
        wl.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.presets: list[tuple[QPushButton, str]] = []
        for i, (name, value) in enumerate(schedule.PRESETS):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setProperty("segment", True)
            b.setToolTip(value)
            b.clicked.connect(lambda _c=False, v=value: self.schedule.setText(v))
            self.presets.append((b, value))
            grid.addWidget(b, i // 4, i % 4)
        wl.addLayout(grid)

        form = QHBoxLayout()
        form.setSpacing(6)
        form.addWidget(label("Repeat", wrap=False))
        self.kind = QComboBox()
        for key, name in ((schedule.INTERVAL, "Every …"), (schedule.HOURLY, "Every hour"),
                          (schedule.DAILY, "Every day"), (schedule.WEEKDAYS, "Monday to Friday"),
                          (schedule.WEEKLY, "Every week"), (schedule.MONTHLY, "Every month"),
                          (schedule.CUSTOM, "Custom (cron)")):
            self.kind.addItem(name, key)
        self.every = spin(1, 99_999, 15)
        self.unit = QComboBox()
        for key, name in (("s", "seconds"), ("m", "minutes"), ("h", "hours"), ("d", "days")):
            self.unit.addItem(name, key)
        self.on_label = label("on", wrap=False)
        self.weekday = QComboBox()
        for i, day in enumerate(schedule.DAYS):
            self.weekday.addItem(day, i)
        self.day = spin(1, 31, 1)
        self.day.setPrefix("day ")
        self.at_label = label("at", wrap=False)
        self.time = QTimeEdit(QTime(9, 0))
        self.time.setDisplayFormat("HH:mm")
        self.minute = spin(0, 59, 0)
        self.minute.setPrefix(":")
        self.past_label = label("past the hour", wrap=False)
        self.custom_label = _muted("Write it below: minute hour day month weekday")
        self.custom_label.setWordWrap(False)
        for w in (self.kind, self.every, self.unit, self.on_label, self.weekday, self.day, self.at_label, self.time,
                  self.minute, self.past_label, self.custom_label):
            form.addWidget(w)
        form.addStretch(1)
        wl.addLayout(form)

        row = QHBoxLayout()
        row.addWidget(label("As text", wrap=False))
        self.schedule = line(text, "every 15m, or cron like 0 * * * *")
        self.schedule.setFont(theme.mono_font(10))
        row.addWidget(self.schedule, 1)
        wl.addLayout(row)
        self.next_runs = _muted("")
        wl.addWidget(self.next_runs)
        wl.addWidget(_muted("An interval counts from when the last run ended. A run that is still going when its "
                            "time comes is left to finish; it runs again at the time after."))
        layout.addWidget(self.when_box)

        # restarts
        self.restart_box = card("RestartCard")
        rl = QVBoxLayout(self.restart_box)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(8)
        rl.addWidget(_title("Restarts"))
        rrow = QHBoxLayout()
        rrow.addWidget(label("Restart", wrap=False))
        self.restart = Segmented((("on-failure", "When it crashes"), ("always", "Always"), ("never", "Never")),
                                 str(d.get("restart", "on-failure")))
        rrow.addWidget(self.restart, 1)
        rl.addLayout(rrow)
        self.restart_note = _muted("")
        rl.addWidget(self.restart_note)
        give = QHBoxLayout()
        give.addWidget(label("Give up after", wrap=False))
        self.max_restarts = spin(0, 1000, int(d.get("max_restarts", 5)))
        self.max_restarts.setSpecialValueText("No limit")
        give.addWidget(self.max_restarts)
        self.give_note = _muted("restarts within 10 minutes. The wait doubles each time, up to a minute.")
        give.addWidget(self.give_note, 1)
        rl.addLayout(give)
        self.scheduled_note = _muted("A scheduled job isn't restarted: after a crash it runs again at its next time.")
        self.scheduled_note.setStyleSheet(f"color: {theme.color('warn')}")
        rl.addWidget(self.scheduled_note)
        stop = QHBoxLayout()
        stop.addWidget(label("Time to stop", wrap=False))
        self.stop_timeout = dspin(0.5, 600, float(d.get("stop_timeout", 10)), suffix=" s")
        stop.addWidget(self.stop_timeout)
        stop.addWidget(_muted("After asking it to stop (SIGTERM / Ctrl+C), the whole process tree is killed."), 1)
        rl.addLayout(stop)
        layout.addWidget(self.restart_box)
        layout.addStretch(1)

        for signal in (self.kind.currentIndexChanged, self.unit.currentIndexChanged,
                       self.weekday.currentIndexChanged):
            signal.connect(self._from_form)
        for box in (self.every, self.day, self.minute):
            box.valueChanged.connect(self._from_form)
        self.time.timeChanged.connect(self._from_form)
        self.schedule.textChanged.connect(self._from_text)
        self.run_mode.changed.connect(self._mode_changed)
        self.restart.changed.connect(self._restart_changed)
        self._from_text()
        self._mode_changed()
        self._restart_changed()

    # -- the form and the text, kept in step ----------------------------------------------------------------

    def _plan(self) -> schedule.Plan:
        t = self.time.time()
        return schedule.Plan(kind=str(self.kind.currentData()), every=self.every.value(),
                             unit=str(self.unit.currentData()), hour=t.hour(),
                             minute=self.minute.value() if self.kind.currentData() == schedule.HOURLY else t.minute(),
                             weekday=int(self.weekday.currentData()), day=self.day.value(),
                             text=self.schedule.text())

    def _show_fields(self, kind: str) -> None:
        timed = kind in (schedule.DAILY, schedule.WEEKDAYS, schedule.WEEKLY, schedule.MONTHLY)
        shown: dict[QWidget, bool] = {
            self.every: kind == schedule.INTERVAL, self.unit: kind == schedule.INTERVAL,
            self.on_label: kind in (schedule.WEEKLY, schedule.MONTHLY), self.weekday: kind == schedule.WEEKLY,
            self.day: kind == schedule.MONTHLY, self.at_label: timed or kind == schedule.HOURLY,
            self.time: timed, self.minute: kind == schedule.HOURLY, self.past_label: kind == schedule.HOURLY,
            self.custom_label: kind == schedule.CUSTOM,
        }
        for w, on in shown.items():
            w.setVisible(on)

    def _from_form(self) -> None:
        if self._syncing:
            return
        plan = self._plan()
        self._show_fields(plan.kind)
        if plan.kind == schedule.CUSTOM:
            self.schedule.setFocus()
            self._preview()
            return
        self._syncing = True
        self.schedule.setText(plan.to_text())
        self._syncing = False
        self._preview()

    def _from_text(self) -> None:
        if self._syncing:
            return
        text = self.schedule.text()
        plan = schedule.read(text) if text.strip() else schedule.Plan()
        self._syncing = True
        self.kind.setCurrentIndex(max(0, self.kind.findData(plan.kind)))
        if plan.kind == schedule.INTERVAL and text.strip():
            self.every.setValue(plan.every)
            self.unit.setCurrentIndex(max(0, self.unit.findData(plan.unit)))
        if plan.kind == schedule.HOURLY:
            self.minute.setValue(plan.minute)
        elif plan.kind != schedule.CUSTOM:
            self.time.setTime(QTime(plan.hour, plan.minute))
            self.weekday.setCurrentIndex(plan.weekday)
            self.day.setValue(plan.day)
        self._syncing = False
        self._show_fields(plan.kind)
        self._preview()

    def _preview(self) -> None:
        text = self.schedule.text().strip()
        for b, value in self.presets:
            b.setChecked(value == text)
        if not text:
            self.summary.setText("")
            self.next_runs.setText("Pick a preset, fill in the form or write a schedule.")
            self.next_runs.setStyleSheet(f"color: {theme.color('muted')}")
            self.schedule.setStyleSheet("")
            return
        try:
            runs = schedule.upcoming(" ".join(text.split()), datetime.now())
        except ValueError as exc:
            self.summary.setText("")
            self.next_runs.setText(str(exc))
            self.next_runs.setStyleSheet(f"color: {theme.color('error')}")
            self.schedule.setStyleSheet(f"border-color: {theme.color('error')}")
            return
        self.schedule.setStyleSheet("")
        self.summary.setText(schedule.describe(text))
        self.summary.setStyleSheet(f"font-weight: 700; color: {theme.color('accent_hi')};")
        today = datetime.now().date()
        plan = schedule.read(text)
        seconds = plan.kind == schedule.INTERVAL and plan.unit == "s"

        def when(t: datetime) -> str:
            day = ("today" if t.date() == today else "tomorrow" if (t.date() - today).days == 1
                   else t.strftime("%a %d %b").replace(" 0", " "))
            return f"{day} {t:%H:%M:%S}" if seconds else f"{day} {t:%H:%M}"

        self.next_runs.setText("Next: " + "  ·  ".join(when(t) for t in runs))
        self.next_runs.setStyleSheet(f"color: {theme.color('ok')}")

    # -- the rest -------------------------------------------------------------------------------------------

    def scheduled(self) -> bool:
        return self.run_mode.value() == SCHEDULED

    def _mode_changed(self, *_: Any) -> None:
        on = self.scheduled()
        if on and not self.schedule.text().strip():
            self.schedule.setText("every 15m")
        self.when_box.setVisible(on)
        self.autostart.setText("Also run it once when Pythonator starts" if on else "Start when Pythonator starts")
        self._restart_changed()

    def _restart_changed(self, *_: Any) -> None:
        on = self.scheduled()
        self.restart.setEnabled(not on)
        self.max_restarts.setEnabled(not on and self.restart.value() != "never")
        self.give_note.setEnabled(not on)
        self.restart_note.setVisible(not on)
        self.scheduled_note.setVisible(on)
        self.restart_note.setText(_RESTART_NOTES.get(self.restart.value(), ""))

    def schedule_text(self) -> str:
        """The schedule to save: empty when kept running. Raises ValueError for a schedule left empty."""
        if not self.scheduled():
            return ""
        text = self.schedule.text().strip()
        if not text:
            raise ValueError("Schedule: pick when it runs, or choose Keep it running")
        return text
