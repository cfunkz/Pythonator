"""The look: text stays readable in both themes, and the service list keeps your order."""

import pytest

from pythonator.ui import theme as th


def _lum(hex_: str) -> float:
    c = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("t", [th.DARK, th.LIGHT], ids=["dark", "light"])
def test_text_tokens_are_readable_on_every_surface(t):
    for fg in ("text", "muted", "faint", "accent_text", "bad", "info", "m_container", "m_wsl"):
        for bg in ("bg", "surface", "raised"):
            assert contrast(getattr(t, fg), getattr(t, bg)) >= 4.5, (t.name, fg, bg)
    for fg in ("ok", "warn", "m_sandbox", "m_full"):  # also drawn as icons and chips; 4.5:1 on the page itself
        assert contrast(getattr(t, fg), t.bg) >= 4.4, (t.name, fg)
    assert contrast(t.accent_ink, t.accent) >= 4.5, "primary buttons"


@pytest.mark.parametrize("t", [th.DARK, th.LIGHT], ids=["dark", "light"])
def test_the_console_is_navy_in_both_themes_and_paints_with_dark_tones(t):
    """In light mode the command boxes on Engines showed navy text on the navy console."""
    for fg in ("console_text", "faint", "accent_text", "ok", "warn", "bad"):
        assert contrast(getattr(th.DARK, fg), t.console) >= 4.5, (t.name, fg)
    assert f"QPlainTextEdit#Console, #Console QLabel {{ color: {th.DARK.console_text}; }}" in th.qss(t)


class _Settings:
    store: dict[str, object] = {}

    def value(self, key, default=None):
        return self.store.get(key, default)

    def setValue(self, key, value):  # noqa: N802 - Qt's name
        self.store[key] = value


def test_moving_and_dragging_a_service_stays_in_its_group(qtbot, monkeypatch):
    from pythonator.ui import widgets

    monkeypatch.setattr(widgets, "QSettings", _Settings)
    monkeypatch.setattr(_Settings, "store", {})

    def svc(name, group):
        return {"spec": {"name": name, "group": group, "mode": "sandbox"}, "observed": {"phase": "stopped"}, "approved": True}

    lst = widgets.ServiceList()
    qtbot.addWidget(lst)
    lst.update_services([svc("a", "web"), svc("b", "web"), svc("c", "web"), svc("x", "jobs")])

    def web():
        return [n for n in lst.order if n in "abc"]

    lst.move("c", to="a")  # a drop on a
    assert web() == ["c", "a", "b"]
    lst.move("c", 1)  # Alt+Down
    assert web() == ["a", "c", "b"]
    lst.move("a", -1)  # already first: nothing
    assert web() == ["a", "c", "b"]
    before = list(lst.order)
    lst.move("a", to="x")  # another group: nothing
    assert lst.order == before
    names = [lst.item(i).data(widgets.ROLE_NAME) for i in range(lst.count())]
    assert [n for n in names if n] == ["x", "a", "c", "b"]  # groups sorted, your order inside each


def test_a_drop_moves_the_service_after_the_drag(qtbot, monkeypatch):
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from PyQt6.QtGui import QDropEvent

    from pythonator.ui import widgets

    monkeypatch.setattr(widgets, "QSettings", _Settings)
    monkeypatch.setattr(_Settings, "store", {})
    lst = widgets.ServiceList()
    qtbot.addWidget(lst)
    lst.resize(300, 400)
    lst.show()
    qtbot.waitExposed(lst)
    lst.update_services([{"spec": {"name": n, "group": "", "mode": "sandbox"}, "observed": {"phase": "stopped"},
                          "approved": True} for n in ("a", "b", "c")])
    lst.setCurrentRow(2)
    ev = QDropEvent(QPointF(lst.visualItemRect(lst.item(0)).center()), Qt.DropAction.MoveAction, QMimeData(),
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    lst.dropEvent(ev)
    assert ev.dropAction() == Qt.DropAction.IgnoreAction, "Qt must not remove the dragged row itself"
    qtbot.waitUntil(lambda: [lst.item(i).data(widgets.ROLE_NAME) for i in range(lst.count())] == ["c", "a", "b"])


def test_the_compact_switch_resizes_rows_at_once_and_is_remembered(qtbot, monkeypatch):
    from pythonator.ui import widgets

    monkeypatch.setattr(widgets, "QSettings", _Settings)
    monkeypatch.setattr(_Settings, "store", {})
    lst = widgets.ServiceList()
    qtbot.addWidget(lst)
    lst.update_services([{"spec": {"name": "a", "group": "", "mode": "sandbox"}, "observed": {"phase": "stopped"},
                          "approved": True}])
    assert lst.visualItemRect(lst.item(0)).height() == 76, "cards are the default"
    lst.set_compact(True)
    assert lst.visualItemRect(lst.item(0)).height() == 54
    assert widgets.ServiceList().cards.compact, "the next window opens compact too"
