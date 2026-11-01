"""Tabbed editor with find/replace, external-change detection and safe (atomic) saving."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QSaveFile, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import keys, theme
from .code_editor import HAS_JEDI, CodeEditor, jedi_hint
from .highlighter import language_for
from .languages import BY_NAME as _SPECS

_FILE_FILTER = (
    "Code (*.py *.pyw *.js *.mjs *.cjs *.ts *.mts *.cts *.json *.go *.java *.cs *.rb *.php *.sh *.bat *.cmd *.ps1 "
    "*.rs *.toml *.yaml *.yml *.env *.md *.txt);;Python (*.py *.pyw);;"
    "JavaScript / TypeScript (*.js *.mjs *.cjs *.ts *.mts *.cts);;All files (*)"
)
MAX_OPEN_BYTES = 20 * 1024 * 1024  # bigger files (logs, dumps) freeze a text widget; not worth it

# Languages offered by "New", with the extension used when the file is first saved.
NEW_FILE_KINDS = (
    ("Python file", "python", ".py"),
    ("JavaScript file", "javascript", ".js"),
    ("Go file", "go", ".go"),
    ("Ruby script", "ruby", ".rb"),
    ("PHP script", "php", ".php"),
    ("Shell script", "shell", ".sh"),
    ("PowerShell script", "powershell", ".ps1"),
)
_EXTENSION_OF = {language: ext for _, language, ext in NEW_FILE_KINDS}

NEW_FILE_TEMPLATE = '''"""New script."""


def main() -> None:
    print("Hello from Pythonator")


if __name__ == "__main__":
    main()
'''


NEW_JS_TEMPLATE = """// New script.

function main() {
  console.log("Hello from Pythonator");
}

main();
"""


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser().absolute())


class EditorTab(CodeEditor):
    def __init__(self, path: str | None) -> None:
        super().__init__()
        self.path = path
        self.disk_text = ""

    @property
    def title(self) -> str:
        return Path(self.path).name if self.path else "untitled"

    def is_modified(self) -> bool:
        doc = self.document()
        return bool(doc and doc.isModified())

    def set_modified(self, modified: bool) -> None:
        if doc := self.document():
            doc.setModified(modified)


class FindBar(QWidget):
    def __init__(self, panel: EditorPanel) -> None:
        super().__init__()
        self.setObjectName("FindBar")
        self._panel = panel
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace")
        self.case = QCheckBox("Aa")
        self.case.setToolTip("Match case")
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        buttons = []
        for label, tip, slot in (
            ("↑", "Previous (Shift+Enter)", lambda: self.step(backward=True)),
            ("↓", "Next (Enter)", lambda: self.step(backward=False)),
            ("Replace", "Replace current match", self.replace_one),
            ("All", "Replace all matches", self.replace_all),
            ("✕", "Close (Esc)", self.close_bar),
        ):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            buttons.append(b)
        layout.addWidget(self.find_edit, 2)
        layout.addWidget(buttons[0])
        layout.addWidget(buttons[1])
        layout.addWidget(self.case)
        layout.addWidget(self.replace_edit, 2)
        layout.addWidget(buttons[2])
        layout.addWidget(buttons[3])
        layout.addWidget(self.status, 1)
        layout.addWidget(buttons[4])
        self.find_edit.returnPressed.connect(lambda: self.step(backward=False))
        self.replace_edit.returnPressed.connect(self.replace_one)
        self.find_edit.textChanged.connect(lambda _t: self._incremental())
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close_bar, context=ctx)
        QShortcut(QKeySequence("Shift+Return"), self.find_edit, lambda: self.step(backward=True),
                  context=Qt.ShortcutContext.WidgetShortcut)

    def _flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        return flags

    def open_bar(self, replace: bool = False) -> None:
        self.show()
        editor = self._panel.current()
        if editor and (sel := editor.textCursor().selectedText()) and "\u2029" not in sel:
            self.find_edit.setText(sel)
        (self.replace_edit if replace and self.find_edit.text() else self.find_edit).setFocus()
        self.find_edit.selectAll()

    def close_bar(self) -> None:
        self.hide()
        if editor := self._panel.current():
            editor.setFocus()

    def _incremental(self) -> None:
        editor = self._panel.current()
        if editor is None:
            return
        cursor = editor.textCursor()
        cursor.setPosition(cursor.selectionStart())
        editor.setTextCursor(cursor)
        self.step(backward=False)

    def step(self, *, backward: bool) -> bool:
        editor = self._panel.current()
        needle = self.find_edit.text()
        if editor is None or not needle:
            self.status.setText("")
            return False
        if editor.find(needle, self._flags(backward)):
            self.status.setText("")
            return True
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
        editor.setTextCursor(cursor)
        found = editor.find(needle, self._flags(backward))
        self.status.setText("" if found else "No matches")
        return found

    def _matches(self, cursor: QTextCursor) -> bool:
        sel = cursor.selectedText()
        needle = self.find_edit.text()
        return sel == needle if self.case.isChecked() else sel.lower() == needle.lower()

    def replace_one(self) -> None:
        editor = self._panel.current()
        if editor is None or not self.find_edit.text():
            return
        cursor = editor.textCursor()
        if cursor.hasSelection() and self._matches(cursor):
            cursor.insertText(self.replace_edit.text())
        self.step(backward=False)

    def replace_all(self) -> None:
        editor = self._panel.current()
        needle = self.find_edit.text()
        doc = editor.document() if editor else None
        if editor is None or doc is None or not needle:
            return
        count = 0
        cursor = QTextCursor(doc)
        edit = QTextCursor(doc)
        edit.beginEditBlock()
        while not (cursor := doc.find(needle, cursor, self._flags())).isNull():
            cursor.insertText(self.replace_edit.text())
            count += 1
        edit.endEditBlock()
        self.status.setText(f"Replaced {count}")


class EditorPanel(QWidget):
    status_message = pyqtSignal(str)
    cursor_info = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._python_for: dict[str, str | None] = {}
        self._jedi_hinted = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(False)  # we add our own flat close buttons
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        if bar := self.tabs.tabBar():
            bar.setDrawBase(False)
            bar.setExpanding(False)
        self.tabs.currentChanged.connect(self._on_current_changed)

        corner = theme.named(QWidget(), "EditorCorner")
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(2)
        new_btn = QToolButton()
        new_btn.setText("New")
        new_btn.setToolTip("New file")
        new_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        new_menu = QMenu(new_btn)
        for label, language, _ext in NEW_FILE_KINDS:
            action = new_menu.addAction(label)
            if action is not None:
                action.triggered.connect(lambda _c=False, lang=language: self.new_file(lang))
        new_btn.setMenu(new_menu)
        cl.addWidget(new_btn)
        for label, tip, slot in (
            ("Open…", f"Open a file ({keys.text(keys.OPEN)})", self.open_dialog),
            ("Save", f"Save ({keys.text(keys.SAVE)})", self.save_current),
        ):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            cl.addWidget(b)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)

        self._placeholder = QLabel(
            f"<div style='color:{theme.TEXT_FAINT}'><p style='font-size:15px'>No file open</p>"
            "<p>Select a service to open its script, or use <b>Open…</b> / <b>New</b>.</p>"
            f"<p>{keys.text(keys.START)} start / restart · {keys.text(keys.STOP)} stop · {keys.text(keys.SAVE)} save"
            f" · {keys.text(keys.FIND)} find · {keys.text(keys.COMMENT)} comment</p></div>"
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._placeholder)
        self._stack.addWidget(self.tabs)
        layout.addWidget(self._stack, 1)
        self.find_bar = FindBar(self)
        self.find_bar.hide()
        layout.addWidget(self.find_bar)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence.StandardKey.Save, self, self.save_current, context=ctx)
        QShortcut(QKeySequence(keys.SAVE_AS), self, self.save_as_current, context=ctx)
        QShortcut(QKeySequence.StandardKey.Open, self, self.open_dialog, context=ctx)
        QShortcut(QKeySequence.StandardKey.Find, self, lambda: self.find_bar.open_bar(), context=ctx)
        # The platform's own Replace key too, but each key once: two shortcuts on one key cancel out.
        replace = QKeySequence.keyBindings(QKeySequence.StandardKey.Replace)
        for seq in keys.unique(*keys.sequences(keys.REPLACE), *replace):
            QShortcut(seq, self, lambda: self.find_bar.open_bar(True), context=ctx)
        QShortcut(QKeySequence(keys.CLOSE_TAB), self, lambda: self.close_tab(self.tabs.currentIndex()), context=ctx)
        QShortcut(QKeySequence(keys.GO_TO_LINE), self, self._go_to_line_prompt, context=ctx)
        self._sync_stack()

    # -- tabs -----------------------------------------------------------------------------

    def editors(self) -> list[EditorTab]:
        return [w for i in range(self.tabs.count()) if isinstance(w := self.tabs.widget(i), EditorTab)]

    def current(self) -> EditorTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, EditorTab) else None

    def find_tab(self, path: str) -> EditorTab | None:
        key = _norm(path)
        return next((e for e in self.editors() if e.path and _norm(e.path) == key), None)

    def set_python_for(self, path: str, python: str | None) -> None:
        """Use ``python`` (a service's interpreter) for completions in ``path``."""
        self._python_for[_norm(path)] = python
        if tab := self.find_tab(path):
            tab.python = python

    def open_file(self, path: str, line: int | None = None, *, focus: bool = True) -> EditorTab | None:
        tab = self.find_tab(path)
        if tab is None:
            p = Path(path).expanduser()
            if not p.is_file():
                self.status_message.emit(f"File not found: {p}")
                return None
            try:
                if p.stat().st_size > MAX_OPEN_BYTES:
                    self.status_message.emit(f"{p.name} is too large to edit here "
                                             f"({p.stat().st_size / 1e6:.0f} MB). Open it in another editor.")
                    return None
                raw = p.read_bytes()
                if b"\0" in raw[:8192]:
                    self.status_message.emit(f"{p.name} looks like a binary file; not opened.")
                    return None
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                self.status_message.emit(f"{p.name} isn't UTF-8 text; not opened.")
                return None
            except OSError as exc:
                QMessageBox.warning(self, "Open failed", f"Could not open {p}:\n{exc.strerror or exc}")
                return None
            tab = self._add_tab(_norm(p), text)
        self.tabs.setCurrentWidget(tab)
        if line is not None:
            tab.go_to_line(line)
        elif focus:
            tab.setFocus()
        return tab

    def _add_tab(self, path: str | None, text: str, language: str = "python") -> EditorTab:
        tab = EditorTab(path)
        tab.set_language(language_for(path) if path else language)
        if tab.language == "python" and not HAS_JEDI and not self._jedi_hinted:
            self._jedi_hinted = True  # once per session
            self.status_message.emit(jedi_hint())
        tab.setPlainText(text)
        tab.disk_text = text
        tab.set_modified(False)
        if path:
            tab.python = self._python_for.get(path)
            self._watcher.addPath(path)
        if doc := tab.document():
            doc.modificationChanged.connect(lambda _m, t=tab: self._refresh_title(t))
        tab.cursor_moved.connect(lambda ln, col: self.cursor_info.emit(f"Ln {ln}, Col {col}"))
        idx = self.tabs.addTab(tab, tab.title)
        self.tabs.setTabToolTip(idx, path or "Unsaved file")
        close = theme.named(QToolButton(), "TabClose")
        close.setText("×")
        close.setToolTip(f"Close ({keys.text(keys.CLOSE_TAB)})")
        close.clicked.connect(lambda _c=False, t=tab: self.close_tab(self.tabs.indexOf(t)))
        if bar := self.tabs.tabBar():
            bar.setTabButton(idx, QTabBar.ButtonPosition.RightSide, close)
        self._sync_stack()
        return tab

    def path_moved(self, old: str, new: str) -> None:
        """A file or folder was renamed outside the editor: keep its open tabs pointing at it."""
        old_key, new_key = Path(_norm(old)), Path(_norm(new))
        for tab in self.editors():
            if not tab.path:
                continue
            current = Path(tab.path)
            if current == old_key:
                moved = new_key
            elif current.is_relative_to(old_key):
                moved = new_key / current.relative_to(old_key)
            else:
                continue
            if tab.path in self._watcher.files():
                self._watcher.removePath(tab.path)
            tab.path = str(moved)
            self._watcher.addPath(tab.path)
            tab.set_language(language_for(tab.path))
            self._refresh_title(tab)

    def _refresh_title(self, tab: EditorTab) -> None:
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, ("● " if tab.is_modified() else "") + tab.title)
            self.tabs.setTabToolTip(idx, tab.path or "Unsaved file")

    def _sync_stack(self) -> None:
        self._stack.setCurrentIndex(1 if self.tabs.count() else 0)

    def _on_current_changed(self, _index: int) -> None:
        if tab := self.current():
            c = tab.textCursor()
            self.cursor_info.emit(f"Ln {c.blockNumber() + 1}, Col {c.positionInBlock() + 1}")
        else:
            self.cursor_info.emit("")

    def new_file(self, language: str = "python") -> None:
        if language == "javascript":
            template = NEW_JS_TEMPLATE
        elif spec := _SPECS.get(language):
            template = spec.new_file
        else:
            template = NEW_FILE_TEMPLATE
        tab = self._add_tab(None, template, language)
        self.tabs.setCurrentWidget(tab)
        tab.setFocus()

    def open_dialog(self) -> None:
        start = str(Path(cur.path).parent) if (cur := self.current()) and cur.path else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Open file", start, _FILE_FILTER)
        if path:
            self.open_file(path)

    def close_tab(self, index: int) -> bool:
        tab = self.tabs.widget(index)
        if not isinstance(tab, EditorTab):
            return False
        if tab.is_modified():
            self.tabs.setCurrentIndex(index)
            answer = QMessageBox.question(
                self, "Unsaved changes", f"Save changes to {tab.title}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save and not self.save(tab):
                return False
        if tab.path:
            self._watcher.removePath(tab.path)
        self.tabs.removeTab(index)
        tab.deleteLater()
        self._sync_stack()
        return True

    def confirm_close_all(self) -> bool:
        """Ask about unsaved tabs. Returns False if the user cancelled."""
        dirty = [t for t in self.editors() if t.is_modified()]
        if not dirty:
            return True
        names = "\n".join(f"  • {t.path or t.title}" for t in dirty)
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes before closing?\n\n{names}",
            QMessageBox.StandardButton.SaveAll | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.SaveAll:
            return all(self.save(t) for t in dirty)
        return True

    # -- saving ---------------------------------------------------------------------------

    def save_current(self) -> None:
        if tab := self.current():
            self.save(tab)

    def save_as_current(self) -> None:
        if tab := self.current():
            self.save(tab, ask_path=True)

    def save_all(self, paths: set[str] | None = None) -> bool:
        """Save modified tabs (optionally only those in ``paths``)."""
        wanted = {_norm(p) for p in paths} if paths is not None else None
        ok = True
        for tab in self.editors():
            if tab.is_modified() and tab.path and (wanted is None or _norm(tab.path) in wanted):
                ok = self.save(tab) and ok
        return ok

    def save(self, tab: EditorTab, *, ask_path: bool = False) -> bool:
        path = tab.path
        if ask_path or not path:
            ext = _EXTENSION_OF.get(tab.language, ".txt")
            start = path or str(Path.home() / f"script{ext}")
            chosen, _ = QFileDialog.getSaveFileName(self, "Save as", start, _FILE_FILTER)
            if not chosen:
                return False
            if tab.path:
                self._watcher.removePath(tab.path)
            path = tab.path = _norm(chosen)
            tab.set_language(language_for(path))
        text = tab.toPlainText()
        if not text.endswith("\n"):
            text += "\n"
        f = QSaveFile(path)
        ok = (
            f.open(QSaveFile.OpenModeFlag.WriteOnly)
            and f.write(text.encode("utf-8")) != -1
            and f.commit()
        )
        if not ok:
            f.cancelWriting()
            QMessageBox.warning(self, "Save failed", f"Could not save {path}:\n{f.errorString()}")
            return False
        tab.disk_text = text
        tab.set_modified(False)
        self._refresh_title(tab)
        # QSaveFile replaces the file, which drops it from the watcher on some platforms.
        if path not in self._watcher.files():
            self._watcher.addPath(path)
        self.status_message.emit(f"Saved {path}")
        return True

    # -- external changes -----------------------------------------------------------------

    def _on_file_changed(self, path: str) -> None:
        # Editors often save by replace; give the new file a moment to appear.
        QTimer.singleShot(150, lambda: self._reload_from_disk(path))

    def _reload_from_disk(self, path: str) -> None:
        tab = self.find_tab(path)
        if tab is None:
            return
        p = Path(path)
        if p.exists() and path not in self._watcher.files():
            self._watcher.addPath(path)
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if text == tab.disk_text:
            return
        tab.disk_text = text
        if tab.is_modified():
            self.status_message.emit(f"{p.name} changed on disk; your unsaved edits were kept")
            return
        cursor = tab.textCursor()
        pos = cursor.position()
        sb = tab.verticalScrollBar()
        scroll = sb.value() if sb else 0
        tab.setPlainText(text)
        tab.set_modified(False)
        cursor = tab.textCursor()
        cursor.setPosition(min(pos, len(text)))
        tab.setTextCursor(cursor)
        if sb:
            sb.setValue(scroll)
        self.status_message.emit(f"Reloaded {p.name} (changed on disk)")

    def _go_to_line_prompt(self) -> None:
        from PyQt6.QtWidgets import QInputDialog  # noqa: PLC0415

        tab = self.current()
        if tab is None:
            return
        doc = tab.document()
        line, ok = QInputDialog.getInt(
            self, "Go to line", "Line:", tab.textCursor().blockNumber() + 1, 1, doc.blockCount() if doc else 1
        )
        if ok:
            tab.go_to_line(line)
