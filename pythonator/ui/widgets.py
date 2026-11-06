"""Small typed constructors for the widgets every dialog uses (Qt's keyword properties aren't in the stubs)."""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QWidget,
)


def label(text: str = "", *, wrap: bool = True, plain: bool = True, muted: bool = False) -> QLabel:
    """``plain``: never read as HTML. Most labels show something a program, plugin or git wrote, and Qt would
    otherwise guess, so ``<img src=/dev/zero>`` in a name could hang the window."""
    w = QLabel(text)
    w.setWordWrap(wrap)
    if plain:
        w.setTextFormat(Qt.TextFormat.PlainText)
    if muted:
        w.setProperty("muted", True)
    return w


def line(text: str = "", placeholder: str = "", *, password: bool = False) -> QLineEdit:
    w = QLineEdit(text)
    w.setPlaceholderText(placeholder)
    if password:
        w.setEchoMode(QLineEdit.EchoMode.Password)
    return w


def spin(lo: int, hi: int, value: int, *, suffix: str = "", step: int = 1) -> QSpinBox:
    w = QSpinBox()
    w.setRange(lo, hi)
    w.setValue(max(lo, min(hi, value)))
    w.setSuffix(suffix)
    w.setSingleStep(step)
    return w


def dspin(lo: float, hi: float, value: float, *, suffix: str = "") -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setValue(max(lo, min(hi, value)))
    w.setSuffix(suffix)
    return w


def view(text: str = "") -> QPlainTextEdit:
    """A read-only text box."""
    w = QPlainTextEdit(text)
    w.setReadOnly(True)
    return w


def scroll() -> QScrollArea:
    w = QScrollArea()
    w.setWidgetResizable(True)
    return w


def tabs(*, closable: bool = False, movable: bool = False) -> QTabWidget:
    w = QTabWidget()
    w.setDocumentMode(True)
    w.setTabsClosable(closable)
    w.setMovable(movable)
    return w


_OK, _NONE = QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.NoButton
_YES_NO = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No


def warn(parent: QWidget | None, title: str, text: str, buttons: QMessageBox.StandardButton = _OK,
            default: QMessageBox.StandardButton = _NONE, *,
            icon: QMessageBox.Icon = QMessageBox.Icon.Warning) -> QMessageBox.StandardButton:
    """QMessageBox.warning, with the text shown as plain text (it often holds an error from git, a plugin or the
    container engine)."""
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    if default != _NONE:
        box.setDefaultButton(default)
    return QMessageBox.StandardButton(box.exec())


def info(parent: QWidget | None, title: str, text: str) -> QMessageBox.StandardButton:
    return warn(parent, title, text, icon=QMessageBox.Icon.Information)


def ask(parent: QWidget | None, title: str, text: str, buttons: QMessageBox.StandardButton = _YES_NO,
        default: QMessageBox.StandardButton = _NONE) -> QMessageBox.StandardButton:
    return warn(parent, title, text, buttons, default, icon=QMessageBox.Icon.Question)


def tip(text: str) -> str:
    """A tooltip for text from a plugin or a program: Qt reads tooltips as HTML when they look like it."""
    if "<" not in text and "&" not in text:
        return text  # nothing Qt could read as HTML
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"
