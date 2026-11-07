"""The service settings' Limits tab and Schedule & restarts tab, and the schedule builder behind them."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QTime
from PyQt6.QtWidgets import QTabWidget

from pythonator.core import limits, schedule
from pythonator.ui.service_dialog import ServiceDialog

# -- the schedule builder (Qt-free) -------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "kind"), [
    ("every 15m", schedule.INTERVAL), ("every 30s", schedule.INTERVAL), ("every 6h", schedule.INTERVAL),
    ("0 * * * *", schedule.HOURLY), ("5 * * * *", schedule.HOURLY), ("0 3 * * *", schedule.DAILY),
    ("0 9 * * 1-5", schedule.WEEKDAYS), ("30 18 * * 5", schedule.WEEKLY), ("0 0 1 * *", schedule.MONTHLY),
    ("*/5 9-17 * * 1-5", schedule.CUSTOM), ("0 9 * 6 *", schedule.CUSTOM), ("not a schedule", schedule.CUSTOM),
])
def test_a_schedule_reads_into_the_form_and_back(text: str, kind: str) -> None:
    plan = schedule.read(text)
    assert plan.kind == kind and plan.to_text() == text


def test_every_preset_is_a_valid_schedule_the_form_can_show() -> None:
    for name, text in schedule.PRESETS:
        assert schedule.check(text) == text, name
        assert schedule.read(text).kind != schedule.CUSTOM, name


def test_macros_and_sunday_as_seven_read_as_the_form() -> None:
    assert schedule.read("@daily").to_text() == "0 0 * * *"
    assert schedule.read("0 9 * * 7").weekday == 0  # cron's 7 is Sunday too


def test_a_schedule_in_words_and_its_next_runs() -> None:
    assert schedule.describe("") == "Keeps running"
    assert schedule.describe("every 1h") == "Every hour"
    assert schedule.describe("every 15m") == "Every 15 minutes"
    assert schedule.describe("0 9 * * 1-5") == "Monday to Friday at 09:00"
    assert schedule.describe("30 18 * * 5") == "Every Friday at 18:30"
    assert schedule.describe("0 0 22 * *") == "On the 22nd of every month at 00:00"
    assert schedule.describe("0 0 11 * *") == "On the 11th of every month at 00:00"
    assert schedule.describe("*/5 * * * *") == "Cron: */5 * * * *"
    friday = datetime(2026, 11, 6, 12, 0)
    assert schedule.upcoming("0 9 * * 1-5", friday) == [datetime(2026, 11, 9, 9), datetime(2026, 11, 10, 9),
                                                       datetime(2026, 11, 11, 9)]
    with pytest.raises(ValueError, match="10 seconds"):
        schedule.upcoming("every 5s", friday)


# -- the dialog ----------------------------------------------------------------------------------------------------


def _dialog(qtbot: Any, folder: Path, **preset: Any) -> ServiceDialog:
    (folder / "bot.py").write_text("")
    dlg = ServiceDialog(None, [], preset={"name": "bot", "folder": str(folder), "entry": "bot.py",
                                          "runtime": "python", **preset})
    qtbot.addWidget(dlg)
    return dlg


def test_limits_and_schedule_have_their_own_tabs(qtbot: Any, tmp_path: Path) -> None:
    dlg = _dialog(qtbot, tmp_path)
    tabs = dlg.findChild(QTabWidget)
    assert tabs is not None
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "General", "Isolation", "Network", "Limits", "Schedule && restarts", "Environment"]


def test_the_limit_slider_chips_and_profiles_fill_the_box(qtbot: Any, tmp_path: Path) -> None:
    dlg = _dialog(qtbot, tmp_path)
    card = dlg.limits_tab.memory
    assert card.big.text() == "No limit" and card.slider.value() == card.slider.maximum()
    card.slider.setValue(0)  # the smallest stop
    assert dlg.memory.text() == "64 MB" and dlg.build().memory_mb == 64
    card.slider.setValue(card.slider.maximum())
    assert dlg.memory.text() == "" and dlg.build().memory_mb == 0
    dlg.memory.setText("1G")
    assert card.big.text() == "1 GB" and "1 GB" in dlg.memory_note.text()
    assert card.stops[card.slider.value()] == 1024  # the slider follows what is typed
    chip = next(c for c, v in card.chips if v == "512 MB")
    chip.click()
    assert dlg.memory.text() == "512 MB" and chip.isChecked()
    assert not next(c for c, v in card.chips if v == "").isChecked()
    small = next(b for b in dlg.limits_tab.profiles if b.text() == "Small bot")
    small.click()
    service = dlg.build()
    assert (service.memory_mb, service.cpu_percent) == (256, 25)
    dlg.cpu.setText("lots")
    assert dlg.limits_tab.cpu.big.text() == "Not a number"
    with pytest.raises(ValueError, match="CPU limit"):
        dlg.build()


def test_a_limit_from_a_bigger_computer_is_kept_when_left_alone(qtbot: Any, tmp_path: Path) -> None:
    huge = limits.memory_total_mb() * 2
    dlg = _dialog(qtbot, tmp_path, memory_mb=huge, cpu_percent=limits.cores() * 100 + 100)
    service = dlg.build()
    assert service.memory_mb == huge and service.cpu_percent == limits.cores() * 100 + 100


def test_keep_running_saves_no_schedule_and_restart_rules(qtbot: Any, tmp_path: Path) -> None:
    dlg = _dialog(qtbot, tmp_path, restart="always", max_restarts=3)
    tab = dlg.schedule_tab
    assert not tab.scheduled() and tab.when_box.isHidden() and tab.restart.isEnabled()
    tab.restart.buttons["never"].click()
    assert not tab.max_restarts.isEnabled() and "until you start it" in tab.restart_note.text()
    service = dlg.build()
    assert service.schedule == "" and service.restart.value == "never" and service.max_restarts == 3


def test_a_schedule_from_a_preset_the_form_or_text(qtbot: Any, tmp_path: Path) -> None:
    dlg = _dialog(qtbot, tmp_path)
    tab = dlg.schedule_tab
    tab.run_mode.buttons["scheduled"].click()
    assert tab.scheduled() and not tab.when_box.isHidden() and not tab.restart.isEnabled()
    assert dlg.schedule.text() == "every 15m"  # a start, never an empty schedule
    assert "Also run it once" in dlg.autostart.text()
    preset = next(b for b, v in tab.presets if v == "0 9 * * 1-5")
    preset.click()
    assert dlg.schedule.text() == "0 9 * * 1-5" and preset.isChecked()
    assert tab.kind.currentData() == schedule.WEEKDAYS and tab.time.time() == QTime(9, 0)
    assert tab.summary.text() == "Monday to Friday at 09:00" and tab.next_runs.text().startswith("Next: ")
    tab.time.setTime(QTime(7, 30))  # the form writes the text
    assert dlg.schedule.text() == "30 7 * * 1-5" and not preset.isChecked()
    tab.kind.setCurrentIndex(tab.kind.findData(schedule.MONTHLY))
    tab.day.setValue(15)
    assert dlg.schedule.text() == "30 7 15 * *"
    tab.kind.setCurrentIndex(tab.kind.findData(schedule.INTERVAL))
    tab.every.setValue(2)
    tab.unit.setCurrentIndex(tab.unit.findData("h"))
    assert dlg.schedule.text() == "every 2h" and tab.summary.text() == "Every 2 hours"
    dlg.schedule.setText("*/5 9-17 * * 1-5")  # the text sets the form
    assert tab.kind.currentData() == schedule.CUSTOM and not tab.custom_label.isHidden()
    assert dlg.build().schedule == "*/5 9-17 * * 1-5"
    dlg.schedule.setText("every 5s")
    assert "10 seconds" in tab.next_runs.text()
    with pytest.raises(ValueError, match="10 seconds"):
        dlg.build()
    dlg.schedule.setText("")
    with pytest.raises(ValueError, match="pick when it runs"):
        dlg.build()
    tab.run_mode.buttons["keep"].click()
    assert dlg.build().schedule == ""


def test_a_saved_schedule_opens_on_its_form(qtbot: Any, tmp_path: Path) -> None:
    dlg = _dialog(qtbot, tmp_path, schedule="30 18 * * 5", autostart=True)
    tab = dlg.schedule_tab
    assert tab.scheduled() and tab.kind.currentData() == schedule.WEEKLY
    assert tab.weekday.currentText() == "Friday" and tab.time.time() == QTime(18, 30)
    service = dlg.build()
    assert service.schedule == "30 18 * * 5" and service.autostart
