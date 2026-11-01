"""Confirmation dialogs for actions that could harm this computer or lose data.

Every dialog shows its text as plain text: file names, commands and service names come from
the outside world and must not be rendered as HTML.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QMessageBox, QWidget

from ..core.paths import APP_NAME


def ask(
    parent: QWidget | None,
    title: str,
    text: str,
    details: str = "",
    *,
    accept: str,
    reject: str = "Cancel",
    icon: QMessageBox.Icon = QMessageBox.Icon.Warning,
    remember: str = "",
) -> tuple[bool, bool]:
    """Ask a yes/no question whose safe answer is the default. Returns ``(accepted, remember)``."""
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    if details:
        box.setInformativeText(details)
        if label := box.findChild(QWidget, "qt_msgbox_informativelabel"):
            label.setProperty("textFormat", Qt.TextFormat.PlainText)
    yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(reject, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    check: QCheckBox | None = None
    if remember:
        check = QCheckBox(remember)
        box.setCheckBox(check)
    box.exec()
    accepted = box.clickedButton() is yes
    return accepted, accepted and check is not None and check.isChecked()


def confirm_elevated(who: str) -> tuple[bool, bool]:
    """Started as root / Administrator: continue? Returns ``(continue, don't warn again)``."""
    return ask(
        None, APP_NAME,
        f"{APP_NAME} is running as {who}.",
        f"Every service, $ shell command and package install it starts gets full control of this computer, "
        f"so a bug or a malicious package could change or delete anything, including system files.\n\n"
        f"Unless a service really needs it, quit and start {APP_NAME} as your normal user.",
        accept=f"Continue as {who}", reject="Quit", remember="Don't warn me again",
    )


def confirm_exposed_autostart(reason: str, count: int) -> bool:
    """The settings could have been changed by another user: start the autostart services anyway?"""
    accepted, _ = ask(
        None, APP_NAME,
        f"Autostart is paused: {reason}.",
        "The settings decide which programs run, so another account on this computer could have "
        f"changed them to run its own code as you. Make sure only your account can change that "
        f"folder, and check the {count} autostart service{'s' if count != 1 else ''} before starting "
        f"{'them' if count != 1 else 'it'}.",
        accept="Start them anyway", reject="Don't start",
    )
    return accepted


def confirm_risky_command(parent: QWidget | None, command: str, reason: str) -> bool:
    accepted, _ = ask(
        parent, "Run this command?",
        f"This command {reason}:\n\n{command}",
        "It runs with your account's permissions and can't be undone.",
        accept="Run it",
    )
    return accepted


def confirm_install(parent: QWidget | None, service: str, what: str, folder: str) -> tuple[bool, bool]:
    """Installing runs code from the internet. Returns ``(install, don't ask again for this service)``."""
    return ask(
        parent, "Install dependencies",
        f"Install the packages {what} for “{service}”?",
        f"Project folder: {folder}\n\n"
        "Packages are downloaded from the internet, and installing them can run their own setup code "
        "(npm install scripts, setup.py builds, native extensions) with your account's permissions. "
        "Only continue if you trust this project and the packages it lists.",
        icon=QMessageBox.Icon.Question, accept="Install", remember="Don't ask again for this service",
    )
