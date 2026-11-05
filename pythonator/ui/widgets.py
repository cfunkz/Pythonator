"""Small typed constructors for the widgets every dialog uses (Qt's keyword properties aren't in the stubs)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDoubleSpinBox, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QSpinBox, QTabWidget


def label(text: str = "", *, wrap: bool = True, plain: bool = False, muted: bool = False) -> QLabel:
    w = QLabel(text)
    w.setWordWrap(wrap)
    if plain:  # anything a program or plugin wrote: never interpreted as HTML
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
