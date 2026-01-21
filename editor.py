"""Python Code Editor - Syntax highlighting, line numbers, and completion."""
from __future__ import annotations
import builtins, keyword, os, re, sys
from pathlib import Path
from typing import Callable, Optional
from PyQt6.QtCore import QEvent, QObject, QRect, QRunnable, QSize, Qt, QSaveFile, QStringListModel, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCloseEvent, QFont, QKeySequence, QPainter, QTextCharFormat, QTextCursor, QTextFormat, QSyntaxHighlighter
from PyQt6.QtWidgets import QCompleter, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QPlainTextEdit, QTextEdit, QVBoxLayout, QWidget, QStyle
from config import BTN

try: import jedi
except ImportError: jedi = None

def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat(); f.setForeground(QColor(color))
    if bold: f.setFontWeight(QFont.Weight.Bold)
    if italic: f.setFontItalic(True)
    return f

class PythonHighlighter(QSyntaxHighlighter):
    _re_ident = re.compile(r"\b[A-Za-z_]\w*\b")
    _re_number = re.compile(r"\b(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b")
    _re_decorator = re.compile(r"(^|\s)(@\w+(?:\.\w+)*)")
    _re_def = re.compile(r"\bdef\s+([A-Za-z_]\w*)")
    _re_class = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
    _re_str = re.compile(r"""(?P<prefix>\b[fFrRuU]{0,2})?(?P<q>['"])(?P<body>(?:\\.|(?!\2).)*)(?P=q)""")

    def __init__(self, doc):
        super().__init__(doc)
        self.f_kw = _fmt("#C792EA", bold=True); self.f_builtin = _fmt("#82AAFF"); self.f_num = _fmt("#F78C6C")
        self.f_deco = _fmt("#FFCB6B"); self.f_class = _fmt("#FFCB6B", bold=True); self.f_def = _fmt("#82AAFF", bold=True)
        self.f_import = _fmt("#89DDFF", bold=True); self.f_self = _fmt("#F07178", bold=True)
        self.f_const = _fmt("#FF5370", bold=True); self.f_str = _fmt("#C3E88D"); self.f_comment = _fmt("#7a8699", italic=True)
        self._kw = set(keyword.kwlist); self._import_kw = {"import", "from", "as"}
        self._builtins = {n for n in dir(builtins) if not n.startswith("_")}; self._selfish = {"self", "global", "nonlocal"}

    def _in_spans(self, i: int, spans: list) -> bool: return any(a <= i < b for a, b in spans)

    def highlightBlock(self, text: str) -> None:
        self.setCurrentBlockState(0); spans = []
        def mark(a, b):
            if a < b: spans.append((a, b)); self.setFormat(a, b-a, self.f_str)
        prev, i = self.previousBlockState(), 0
        if prev in (1, 2):
            delim = "'''" if prev == 1 else '"""'; end = text.find(delim)
            if end == -1: mark(0, len(text)); self.setCurrentBlockState(prev); return
            mark(0, end+3); i = end+3
        while i < len(text):
            s1, s2 = text.find("'''", i), text.find('"""', i)
            if s1 == -1 and s2 == -1: break
            if s2 == -1 or (s1 != -1 and s1 < s2): s, delim, state = s1, "'''", 1
            else: s, delim, state = s2, '"""', 2
            e = text.find(delim, s+3)
            if e == -1: mark(s, len(text)); self.setCurrentBlockState(state); break
            mark(s, e+3); i = e+3
        for m in self._re_str.finditer(text):
            if not self._in_spans(m.start(), spans): mark(m.start(), m.end())
        h, scan_to = text.find("#"), len(text)
        if h != -1 and not self._in_spans(h, spans): self.setFormat(h, len(text)-h, self.f_comment); scan_to = h
        head = text[:scan_to]
        for m in self._re_decorator.finditer(head):
            pos = m.start(2)
            if not self._in_spans(pos, spans): self.setFormat(pos, len(m.group(2)), self.f_deco)
        for rx, fmt, g in [(self._re_def, self.f_def, 1), (self._re_class, self.f_class, 1)]:
            for m in rx.finditer(head):
                pos = m.start(g)
                if not self._in_spans(pos, spans): self.setFormat(pos, len(m.group(g)), fmt)
        for m in self._re_number.finditer(head):
            if not self._in_spans(m.start(), spans): self.setFormat(m.start(), len(m.group(0)), self.f_num)
        for m in self._re_ident.finditer(head):
            pos = m.start()
            if self._in_spans(pos, spans): continue
            w = m.group(0)
            if w in self._import_kw: self.setFormat(pos, len(w), self.f_import)
            elif w in self._kw: self.setFormat(pos, len(w), self.f_kw)
            elif w in self._builtins: self.setFormat(pos, len(w), self.f_builtin)
            elif w in self._selfish: self.setFormat(pos, len(w), self.f_self)
            elif len(w) >= 3 and w.isupper(): self.setFormat(pos, len(w), self.f_const)

class _LineArea(QWidget):
    def __init__(self, editor): super().__init__(editor); self.editor = editor
    def sizeHint(self) -> QSize: return QSize(self.editor._ln_width(), 0)
    def paintEvent(self, e): self.editor._paint_ln(e)

class _JobSignals(QObject): done = pyqtSignal(int, list)

class _CompJob(QRunnable):
    def __init__(self, rid: int, fn: Callable[[], list[str]]): super().__init__(); self.rid, self.fn, self.signals = rid, fn, _JobSignals()
    def run(self) -> None:
        try: items = self.fn()
        except: items = []
        self.signals.done.emit(self.rid, items)

def _fallback(code: str) -> list[str]:
    return list(set(re.findall(r"[A-Za-z_]\w+", code)) | set(keyword.kwlist))

class PythonEditor(QPlainTextEdit):
    AUTO_MIN_PREFIX, AUTO_DELAY, JEDI_MAX = 3, 120, 200_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ln_area = _LineArea(self); self._ctx_path: Optional[Path] = None; self._ctx_root = Path.cwd()
        self._pool = QThreadPool.globalInstance(); self._rid = self._pending_rid = 0; self._pending_pos = -1
        self.blockCountChanged.connect(lambda _: self._sync_margins())
        self.updateRequest.connect(self._update_ln)
        self.cursorPositionChanged.connect(self._hl_line)
        font = QFont("Consolas" if sys.platform.startswith("win") else "Monospace", 11)
        font.setStyleHint(QFont.StyleHint.TypeWriter); self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet("QPlainTextEdit { background: #121212; color: #ddd; border: 1px solid #2b2b2b; border-radius: 2px; selection-background-color: #4688d8; }")
        self._hl = PythonHighlighter(self.document())
        self._comp_model = QStringListModel(self); self._completer = QCompleter(self._comp_model, self)
        self._completer.setWidget(self); self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.activated.connect(self._insert_comp)
        self._debounce = QTimer(self); self._debounce.setSingleShot(True); self._debounce.timeout.connect(self._req_comp)
        act = QAction(self); act.setShortcut(QKeySequence("Ctrl+Space")); act.triggered.connect(self._req_comp); self.addAction(act)
        self._sync_margins(); self._hl_line()

    def set_context(self, fp: Optional[Path]) -> None: self._ctx_path = fp; self._ctx_root = fp.parent if fp else Path.cwd()

    def _ln_width(self) -> int: return 12 + self.fontMetrics().horizontalAdvance("9") * max(2, len(str(self.blockCount())))

    def _sync_margins(self) -> None:
        w = self._ln_width(); self.setViewportMargins(w, 0, 0, 0)
        cr = self.contentsRect(); self._ln_area.setGeometry(QRect(cr.left(), cr.top(), w, cr.height())); self._ln_area.update()

    def resizeEvent(self, e): super().resizeEvent(e); self._sync_margins()

    def _update_ln(self, rect, dy):
        if dy: self._ln_area.scroll(0, dy)
        else: self._ln_area.update(0, rect.y(), self._ln_area.width(), rect.height())
        if rect.contains(self.viewport().rect()): self._sync_margins()

    def _paint_ln(self, event):
        painter = QPainter(self._ln_area); painter.fillRect(event.rect(), QColor("#1a1a1a"))
        painter.setPen(QColor("#2b2b2b")); painter.drawLine(self._ln_area.width()-1, event.rect().top(), self._ln_area.width()-1, event.rect().bottom())
        painter.setPen(QColor("#888")); block = self.firstVisibleBlock(); n = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, self._ln_area.width()-6, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(n+1))
            block = block.next(); n += 1; top = bottom; bottom = top + int(self.blockBoundingRect(block).height())

    def _hl_line(self):
        if self.isReadOnly(): return
        hl = QColor("#4688d8"); hl.setAlpha(35); sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(hl); sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor(); sel.cursor.clearSelection(); self.setExtraSelections([sel])

    def event(self, e):
        if e.type() == QEvent.Type.FocusOut and self._completer.popup().isVisible(): self._completer.popup().hide()
        return super().event(e)

    def keyPressEvent(self, event):
        if self._completer.popup().isVisible() and event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            event.ignore(); return
        super().keyPressEvent(event)
        t = event.text()
        if not t: return
        if t == ".": self._debounce.start(0); return
        if (t.isalnum() or t == "_") and len(self._word_under()) >= self.AUTO_MIN_PREFIX: self._debounce.start(self.AUTO_DELAY)

    def _word_under(self) -> str: tc = self.textCursor(); tc.select(QTextCursor.SelectionType.WordUnderCursor); return tc.selectedText()

    def _insert_comp(self, comp: str) -> None:
        tc = self.textCursor(); tc.select(QTextCursor.SelectionType.WordUnderCursor); tc.removeSelectedText(); tc.insertText(comp); self.setTextCursor(tc)

    def _req_comp(self) -> None:
        tc = self.textCursor(); pos = tc.position(); code = self.toPlainText()
        self._rid += 1; self._pending_rid = self._rid; self._pending_pos = pos
        line, col, root = tc.blockNumber()+1, tc.positionInBlock(), str(self._ctx_root)
        path_str = str(self._ctx_path) if self._ctx_path else None
        def compute() -> list[str]:
            if jedi is None or len(code) > self.JEDI_MAX: return _fallback(code)
            try: return [c.name for c in jedi.Script(code=code, path=path_str, project=jedi.Project(path=root)).complete(line, col) if getattr(c, "name", None)]
            except: return _fallback(code)
        job = _CompJob(self._rid, compute); job.signals.done.connect(self._on_comp); self._pool.start(job)

    def _on_comp(self, rid: int, items: list[str]) -> None:
        if rid != self._pending_rid or self.textCursor().position() != self._pending_pos or not items: return
        self._comp_model.setStringList(sorted(set(map(str, items)))[:250]); self._completer.setCompletionPrefix(self._word_under())
        cr = self.cursorRect(); cr.setWidth(self._completer.popup().sizeHintForColumn(0)+30); self._completer.complete(cr)

class EditorWindow(QWidget):
    TEMPLATE = '"""New Python script."""\n\ndef main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'

    def __init__(self, filepath: Optional[str] = None, parent=None):
        super().__init__(parent); self.filepath: Optional[Path] = Path(filepath) if filepath else None
        self._build_ui(); self._bind_shortcuts()
        if (doc := self.editor.document()): doc.modificationChanged.connect(self._on_mod)
        self._set_file(self.filepath, load=bool(self.filepath and self.filepath.exists()))

    def _build_ui(self) -> None:
        self.resize(900, 600); layout = QVBoxLayout(self); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(8)
        top = QHBoxLayout(); top.setSpacing(8)
        self.path_edit = QLineEdit(); self.path_edit.setPlaceholderText("path/to/file.py (Enter to switch)")
        self.path_edit.setStyleSheet("QLineEdit { padding: 4px 8px; border: 1px solid #333; border-radius: 2px; background: #1a1a1a; } QLineEdit:focus { border-color: #4688d8; }")
        self.path_edit.returnPressed.connect(self._apply_path); top.addWidget(self.path_edit, 1)
        for sp, tip, fn in [(QStyle.StandardPixmap.SP_DialogOpenButton, "Open", self.open_dialog),
                            (QStyle.StandardPixmap.SP_BrowserReload, "Reload", self.reload),
                            (QStyle.StandardPixmap.SP_DialogSaveButton, "Save", self.save),
                            (QStyle.StandardPixmap.SP_DirOpenIcon, "Save As", self.save_as)]:
            b = QPushButton(); b.setIcon(self.style().standardIcon(sp)) if self.style() else None; b.setToolTip(tip)
            b.setFixedWidth(36); b.setStyleSheet(BTN); b.clicked.connect(fn); top.addWidget(b)
        layout.addLayout(top)
        self.editor = PythonEditor(self); layout.addWidget(self.editor)
        bottom = QHBoxLayout(); self.status = QLabel(""); self.status.setStyleSheet("color: rgba(220,220,220,160);"); bottom.addWidget(self.status); bottom.addStretch()
        hint = QLabel("Ctrl+Space Complete · Ctrl+S Save · Ctrl+O Open · F5 Reload"); hint.setStyleSheet("color: rgba(220,220,220,120);"); bottom.addWidget(hint)
        layout.addLayout(bottom)

    def _bind_shortcuts(self) -> None:
        for seq, fn in [(QKeySequence.StandardKey.Save, self.save), (QKeySequence.StandardKey.SaveAs, self.save_as),
                        (QKeySequence.StandardKey.Open, self.open_dialog), (QKeySequence(Qt.Key.Key_F5), self.reload)]:
            a = QAction(self); a.setShortcut(seq); a.triggered.connect(fn); self.addAction(a)

    def _ask(self, title: str, text: str): return QMessageBox.question(self, title, text, QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)

    def _with_unsaved(self, cont: Callable[[], None]) -> None:
        doc = self.editor.document()
        if not doc or not doc.isModified(): cont(); return
        resp = self._ask("Unsaved changes", "Save before continuing?")
        if resp == QMessageBox.StandardButton.Save: self.save(); doc = self.editor.document(); (cont() if doc and not doc.isModified() else None)
        elif resp == QMessageBox.StandardButton.Discard: cont()

    def set_file(self, fp: str) -> None: self._with_unsaved(lambda: self._set_file(Path(fp), load=True))
    def open_dialog(self) -> None: self._with_unsaved(self._open_impl)
    def _open_impl(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(self, "Open", str(self.filepath.parent) if self.filepath else os.getcwd(), "Python (*.py);;All (*)")
        if fp: self._set_file(Path(fp), load=True)
    def save(self) -> None:
        if not self.filepath: self.save_as(); return
        self._save_to(self.filepath)
    def save_as(self) -> None:
        fp, _ = QFileDialog.getSaveFileName(self, "Save as", str(self.filepath) if self.filepath else "script.py", "Python (*.py);;All (*)")
        if fp: self._set_file(Path(fp), load=False); self._save_to(self.filepath)
    def reload(self) -> None:
        if self.filepath: self._with_unsaved(lambda: self._load(self.filepath))

    def _apply_path(self) -> None:
        raw = self.path_edit.text().strip().strip('"')
        if not raw: return
        p = Path(raw).expanduser()
        if not p.is_absolute(): p = ((self.filepath.parent if self.filepath else Path.cwd()) / p).resolve()
        self._with_unsaved(lambda: self._set_file(p, load=p.exists()))

    def _set_file(self, path: Optional[Path], load: bool) -> None:
        self.filepath = path; self.path_edit.setText(str(self.filepath) if self.filepath else ""); self.editor.set_context(self.filepath)
        if load and self.filepath: self._load(self.filepath)
        elif not self.filepath: self.editor.setPlainText(self.TEMPLATE); (doc := self.editor.document()) and doc.setModified(False); self._set_status("new")
        else: (doc := self.editor.document()) and doc.setModified(True); self._on_mod(True)
        self._refresh_title()

    def _load(self, path: Path) -> None:
        try:
            self.editor.setPlainText(path.read_text(encoding="utf-8") if path.exists() else self.TEMPLATE)
            (doc := self.editor.document()) and doc.setModified(False); self._set_status("loaded")
        except Exception as e: QMessageBox.warning(self, "Error", f"Load failed:\n{e}")
        self._refresh_title()

    def _save_to(self, path: Optional[Path]) -> None:
        if not path: return
        try:
            path.parent.mkdir(parents=True, exist_ok=True); text = self.editor.toPlainText().replace("\t", "    ")
            sf = QSaveFile(str(path))
            if not sf.open(sf.OpenModeFlag.WriteOnly | sf.OpenModeFlag.Text): raise OSError("Open failed")
            if sf.write(text.encode("utf-8")) == -1: raise OSError("Write failed")
            if not sf.commit(): raise OSError("Commit failed")
            (doc := self.editor.document()) and doc.setModified(False); self._set_status("saved")
        except Exception as e: QMessageBox.warning(self, "Error", f"Save failed:\n{e}")
        self._refresh_title()

    def _on_mod(self, mod: bool) -> None: self._set_status("modified" if mod else ""); self._refresh_title()
    def _refresh_title(self) -> None:
        name = self.filepath.name if self.filepath else "New File"
        star = "*" if (doc := self.editor.document()) and doc.isModified() else ""
        self.setWindowTitle(f"Editor - {name}{star}")
    def _set_status(self, msg: str) -> None: self.status.setText(f"{self.filepath or 'New'} {'· '+msg if msg else ''}".rstrip())

    def closeEvent(self, event: QCloseEvent) -> None:
        doc = self.editor.document()
        if not doc or not doc.isModified(): event.accept(); return
        resp = self._ask("Unsaved changes", "Save before closing?")
        if resp == QMessageBox.StandardButton.Save: self.save(); doc = self.editor.document(); event.accept() if doc and not doc.isModified() else event.ignore()
        elif resp == QMessageBox.StandardButton.Discard: event.accept()
        else: event.ignore()
