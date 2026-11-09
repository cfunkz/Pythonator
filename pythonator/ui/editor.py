"""Screen 5: files, editor, problems and the AI assistant, for one service.
The window never touches the folder itself: the daemon lists, reads and writes it through safefs (no links), shows
.env files masked and read-only, and runs ruff jailed on the text you are editing (no folder, no network). A quick fix
and an assistant answer are proposals: you see the exact text that changes and apply it, or don't. Applying is an
ordinary edit in the buffer (Ctrl Z undoes it) and is saved with the hash you started from."""

from __future__ import annotations

import bisect
import builtins
import difflib
import functools
import itertools
import keyword
import re
import sys
from typing import TYPE_CHECKING, Any

from PyQt6.Qsci import (
    QsciDocument,
    QsciLexer,
    QsciLexerBash,
    QsciLexerCPP,
    QsciLexerCSharp,
    QsciLexerCSS,
    QsciLexerHTML,
    QsciLexerJava,
    QsciLexerJavaScript,
    QsciLexerJSON,
    QsciLexerMarkdown,
    QsciLexerProperties,
    QsciLexerPython,
    QsciLexerRuby,
    QsciLexerSQL,
    QsciLexerXML,
    QsciLexerYAML,
    QsciScintilla,
)
from PyQt6.QtCore import QPoint, QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QLineEdit,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pythonator.policy.runtimes import RUNTIMES
from pythonator.ui import theme as th
from pythonator.ui.icons import icon
from pythonator.ui.kit import (
    Btn,
    Chip,
    Icon,
    InfoBtn,
    Kbd,
    Note,
    clear,
    esc,
    frame,
    hbox,
    label,
    primary,
    show_if,
    splitter,
    vbox,
)

if TYPE_CHECKING:
    from pythonator.ui.app import Main

PROBLEM = 8  # indicator numbers
PROPOSE_OLD = 9
CODE_EXT = (".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".ps1", ".sh")

def lexer_for(path: str) -> QsciLexer | None:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    table: list[tuple[tuple[str, ...], type[QsciLexer]]] = [
        ((".py", ".pyi", ".pyw"), QsciLexerPython),
        ((".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"), QsciLexerJavaScript), ((".json",), QsciLexerJSON),
        ((".yml", ".yaml"), QsciLexerYAML), ((".md", ".markdown"), QsciLexerMarkdown),
        ((".sh", ".bash", ".zsh"), QsciLexerBash),
        ((".toml", ".ini", ".cfg", ".conf", ".properties"), QsciLexerProperties), ((".html", ".htm"), QsciLexerHTML),
        ((".css",), QsciLexerCSS), ((".sql",), QsciLexerSQL), ((".xml", ".csproj", ".svg"), QsciLexerXML),
        ((".go", ".rs", ".c", ".h", ".cpp", ".hpp"), QsciLexerCPP), ((".java", ".kt"), QsciLexerJava),
        ((".cs",), QsciLexerCSharp), ((".rb",), QsciLexerRuby),
    ]
    if name == ".env" or name.startswith(".env.") or name in ("dockerfile", "makefile", "procfile"):
        return QsciLexerProperties()
    for exts, cls in table:
        if p.endswith(exts):
            return cls()
    return None

def efont(pt: float = 10.5, italic: bool = False) -> QFont:
    """Scintilla sizes text in points, not pixels."""
    f = QFont(th.MONO_FAMILY)
    f.setPointSizeF(pt)
    f.setItalic(italic)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f

def style_lexer(lx: QsciLexer, t: th.Tokens) -> None:
    """Every style on the editor background, coloured by what the lexer says the style is."""
    mono = efont()
    lx.setDefaultPaper(QColor(t.bg))
    lx.setDefaultColor(QColor(t.text))
    lx.setDefaultFont(mono)
    for s in range(128):
        d = (lx.description(s) or "").lower()
        if not d:
            continue
        col, italic = t.text, False
        if "comment" in d:
            col, italic = t.faint, True
        elif "string" in d or "character" in d or "literal" in d or "value" in d:
            col = t.ok
        elif "keyword" in d or "key" == d or "section" in d or "tag" in d:
            col = t.info
        elif "number" in d:
            col = t.warn
        elif "decorator" in d or "preprocessor" in d or "annotation" in d:
            col = t.m_wsl
        elif "class name" in d or "function" in d or "header" in d or "attribute" in d:
            col = t.accent_text
        elif "operator" in d:
            col = t.muted
        lx.setFont(efont(italic=italic), s)
        lx.setColor(QColor(col), s)
        lx.setPaper(QColor(t.bg), s)

class _Indent(QStyledItemDelegate):
    """Indents by depth inside the row, so the selected row is one rounded highlight (Qt's own indent area was
    painted as a second, separate block beside it)."""

    def paint(self, p: Any, opt: Any, index: Any) -> None:
        depth, i = 0, index.parent()
        while i.isValid():
            depth, i = depth + 1, i.parent()
        o = QStyleOptionViewItem(opt)
        o.rect = opt.rect.adjusted(depth * 14, 0, 0, 0)
        super().paint(p, o, index)

class FileTree(QTreeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setIndentation(0)
        self.setItemDelegate(_Indent(self))

class Editor(QsciScintilla):
    def __init__(self) -> None:
        super().__init__()
        self.setUtf8(True)
        self.setMarginType(0, QsciScintilla.MarginType.NumberMargin)
        self.setMarginWidth(0, "00000")
        self.setMarginWidth(1, 0)
        self.setFolding(QsciScintilla.FoldStyle.NoFoldStyle)
        self.setIndentationsUseTabs(False)
        self.setTabWidth(4)
        self.setAutoIndent(True)
        self.setCaretLineVisible(True)
        self.setBraceMatching(QsciScintilla.BraceMatch.SloppyBraceMatch)
        self.setScrollWidth(1)
        self.setScrollWidthTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAnnotationDisplay(QsciScintilla.AnnotationDisplay.AnnotationStandard)
        self.indicatorDefine(QsciScintilla.IndicatorStyle.SquiggleIndicator, PROBLEM)
        self.indicatorDefine(QsciScintilla.IndicatorStyle.FullBoxIndicator, PROPOSE_OLD)
        self.SendScintilla(QsciScintilla.SCI_SETEXTRAASCENT, 4)
        self.SendScintilla(QsciScintilla.SCI_SETEXTRADESCENT, 4)
        self.py_lexer = QsciLexerPython(self)
        self.use_words([])  # completion: the project's modules and names, its packages (files.names)
        self._ac = QTimer(self, singleShot=True, interval=120, timeout=self.complete)
        self.setAutoCompletionThreshold(2)
        self.setAutoCompletionCaseSensitivity(False)
        self.setAutoCompletionReplaceWord(True)
        self.setAutoCompletionUseSingle(QsciScintilla.AutoCompletionUseSingle.AcusNever)
        self.retheme()

    def keyPressEvent(self, e: Any) -> None:
        """Ctrl+= and Ctrl++ zoom in (Scintilla only knows the keypad +, so = was typed), Ctrl+- out, Ctrl+0 resets."""
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        k = e.key()
        if ctrl and k in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.zoomIn()
        elif ctrl and k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoomOut()
        elif ctrl and k == Qt.Key.Key_0:
            self.zoomTo(0)
        else:
            super().keyPressEvent(e)
            if self.lexer() is self.py_lexer and (e.text().isidentifier() or e.text() == "." or e.text().isdigit()):
                self._ac.start()  # once typing pauses

    def retheme(self) -> None:
        t = th.current()
        lx = self.lexer()
        if lx is not None:
            style_lexer(lx, t)
        self.setPaper(QColor(t.bg))
        self.setColor(QColor(t.text))
        self.setFont(efont())
        self.setMarginsBackgroundColor(QColor(t.bg))
        self.setMarginsForegroundColor(QColor(t.faint))
        self.setMarginsFont(efont(10))
        self.setMarginWidth(0, "00000")
        self.setCaretForegroundColor(QColor(t.accent))
        self.setCaretLineBackgroundColor(QColor(t.surface))
        self.setSelectionBackgroundColor(th.mix(t.accent, t.bg, 0.28))
        self.setSelectionForegroundColor(QColor(t.text))
        self.setMatchedBraceBackgroundColor(QColor(t.raised2))
        self.setMatchedBraceForegroundColor(QColor(t.accent_text))
        self.setIndicatorForegroundColor(QColor(t.warn), PROBLEM)
        self.setIndicatorForegroundColor(QColor(t.bad), PROPOSE_OLD)
        self.SendScintilla(QsciScintilla.SCI_INDICSETALPHA, PROPOSE_OLD, 40)
        self.SendScintilla(QsciScintilla.SCI_INDICSETOUTLINEALPHA, PROPOSE_OLD, 0)
        # annotation style 100: a proposal's new text, on a green tint
        base = 100
        self.SendScintilla(QsciScintilla.SCI_STYLESETBACK, base, _bgr(th.mix(t.ok, t.bg, 0.13)))
        self.SendScintilla(QsciScintilla.SCI_STYLESETFORE, base, _bgr(QColor(t.text)))
        self.SendScintilla(QsciScintilla.SCI_STYLESETFONT, base, th.MONO_FAMILY.encode())
        self.SendScintilla(QsciScintilla.SCI_STYLESETSIZE, base, 10)
        self.SendScintilla(QsciScintilla.SCI_STYLESETBACK, base + 1, _bgr(th.mix(t.ok, t.bg, 0.13)))
        self.SendScintilla(QsciScintilla.SCI_STYLESETFORE, base + 1, _bgr(QColor(t.faint)))
        self.SendScintilla(QsciScintilla.SCI_STYLESETITALIC, base + 1, 1)
        self.SendScintilla(QsciScintilla.SCI_STYLESETFONT, base + 1, th.MONO_FAMILY.encode())
        self.SendScintilla(QsciScintilla.SCI_STYLESETSIZE, base + 1, 10)

    def set_file(self, path: str, text: str | QsciDocument, readonly: bool) -> None:
        """A file in its own document, so each tab keeps its own undo and redo. `text` starts a new one."""
        if isinstance(text, QsciDocument):
            self.setDocument(text)
        else:
            self.setDocument(QsciDocument())
            self.setText(text)
            self.SendScintilla(QsciScintilla.SCI_EMPTYUNDOBUFFER)  # Ctrl Z can't undo the loading
            self.setModified(False)
        lx = lexer_for(path)
        self.setLexer(self.py_lexer if isinstance(lx, QsciLexerPython) else lx)  # one, with its completion list
        src = QsciScintilla.AutoCompletionSource  # Python: complete() below; anything else: the file's own words
        self.setAutoCompletionSource(src.AcsNone if isinstance(lx, QsciLexerPython) else src.AcsDocument)
        self.setReadOnly(readonly)
        self.retheme()

    def use_words(self, words: list[str]) -> None:
        """Python files complete from these, Python's keywords, builtins and modules (with what the loaded ones
        define), and the file's own words; other files from their own words only."""
        self.words = words
        self.dotted = sorted({*words, *python_words()})
        self.bare = sorted({w.rsplit(".", 1)[-1] for w in self.dotted})

    def complete(self) -> None:
        """After `a.b.` the names a.b holds; otherwise the names that start with what is typed (and this file's
        words). Sorted lists and a prefix search: no parsing or importing while you type."""
        line, col = self.getCursorPosition()
        m = re.search(r"([A-Za-z_][\w.]*)$", self.text(line)[:col])
        if not m or self.isReadOnly():
            return
        ctx, dot, pre = m[1].rpartition(".")
        if dot:
            i, key = bisect.bisect_left(self.dotted, ctx + "."), ctx + "."
            pool = {w[len(key):].split(".", 1)[0] for w in itertools.takewhile(lambda w: w.startswith(key), self.dotted[i:])}
        elif len(pre) >= 2:
            pool = {*self.bare, *re.findall(r"[A-Za-z_]\w{2,}", self.text()[:400_000])}
        else:
            return
        low = pre.lower()
        hits = sorted((w for w in pool if w.lower().startswith(low) and w != pre), key=str.lower)[:300]
        if hits:
            self.SendScintilla(QsciScintilla.SCI_AUTOCSETIGNORECASE, 1)
            self.SendScintilla(QsciScintilla.SCI_AUTOCSHOW, len(pre.encode()), " ".join(hits).encode())

    def mark_problems(self, probs: list[dict[str, Any]]) -> None:
        self.clearIndicatorRange(0, 0, max(0, self.lines() - 1), len(self.text(max(0, self.lines() - 1))), PROBLEM)
        for p in probs:
            l1, c1 = p["line"] - 1, p["col"] - 1
            l2, c2 = p["end_line"] - 1, p["end_col"] - 1
            if l2 > l1:  # a problem about a whole block (unsorted imports): mark its first line only
                l2, c2 = l1, len(self.text(l1).rstrip("\r\n"))
            if (l1, c1) == (l2, c2):
                c2 = c1 + 1
            self.fillIndicatorRange(l1, c1, l2, c2, PROBLEM)

    def show_proposal(self, old: str, new: str) -> int:
        """Tint the text an edit replaces and show the new text under it, without changing the buffer. -> line."""
        self.clear_proposal()
        text = self.text()
        i = text.find(old)
        if i < 0:
            return -1
        start = len(text[:i].encode("utf-8"))
        end = start + len(old.encode("utf-8"))
        l1, c1 = self.lineIndexFromPosition(start)
        l2, c2 = self.lineIndexFromPosition(end)
        self.fillIndicatorRange(l1, c1, l2, c2, PROPOSE_OLD)
        last = l2 if c2 > 0 or l2 == l1 else l2 - 1
        lines = ["  proposal (not applied yet)", *new.rstrip("\n").splitlines()]
        self.SendScintilla(QsciScintilla.SCI_ANNOTATIONSETTEXT, last, "\n".join(lines).encode("utf-8"))
        styles = bytes([101] * len(lines[0].encode()) + [100] * (len("\n".join(lines).encode()) - len(lines[0].encode())))
        self.SendScintilla(QsciScintilla.SCI_ANNOTATIONSETSTYLES, last, styles)
        return l1

    def clear_proposal(self) -> None:
        last = max(0, self.lines() - 1)
        self.clearIndicatorRange(0, 0, last, len(self.text(last)), PROPOSE_OLD)
        self.clearAnnotations()

    def replace_once(self, old: str, new: str) -> bool:
        text = self.text()
        if text.count(old) != 1:
            return False
        i = text.index(old)
        start = len(text[:i].encode("utf-8"))
        end = start + len(old.encode("utf-8"))
        self.beginUndoAction()
        self.SendScintilla(QsciScintilla.SCI_SETTARGETSTART, start)
        self.SendScintilla(QsciScintilla.SCI_SETTARGETEND, end)
        data = new.encode("utf-8")
        self.SendScintilla(QsciScintilla.SCI_REPLACETARGET, len(data), data)
        self.endUndoAction()
        return True

@functools.cache
def python_words() -> tuple[str, ...]:
    std = sys.stdlib_module_names
    loaded = [f"{m}.{a}" for m in std if m in sys.modules for a in dir(sys.modules[m]) if not a.startswith("_")]
    return (*keyword.kwlist, *(b for b in dir(builtins) if not b.startswith("_")), *(m for m in std if m[0] != "_"), *loaded)

def _bgr(c: QColor) -> int:
    return c.red() | (c.green() << 8) | (c.blue() << 16)

def diff_html(path: str, line: int, old: str, new: str, width: int = 0) -> str:
    t = th.current()
    rows = [f"<div style='color:{t.faint}'>@@ {esc(path)}:{line} @@</div>"]
    for d in difflib.ndiff(old.rstrip("\n").splitlines(), new.rstrip("\n").splitlines()):
        tag, body = d[:2], esc(d[2:]).replace(" ", "&nbsp;")
        if tag == "- ":
            rows.append(f"<div style='background:{th.rgba(t.bad, 0.16)};color:{t.bad}'>-&nbsp;{body}</div>")
        elif tag == "+ ":
            rows.append(f"<div style='background:{th.rgba(t.ok, 0.14)};color:{t.ok}'>+&nbsp;{body}</div>")
        elif tag == "  ":
            rows.append(f"<div style='color:{t.muted}'>&nbsp;&nbsp;{body}</div>")
    return f"<div style='font-family:\"{th.MONO_FAMILY}\";font-size:12px'>{''.join(rows)}</div>"

class Proposal(QFrame):
    """An answer: text, and per edit the exact lines that change, with Apply edit and Reject."""

    def __init__(self, page: EditorPage, text: str, edits: list[dict[str, Any]], title: str = "") -> None:
        super().__init__()
        self.setObjectName("Card")
        self.page = page
        lay = vbox(spacing=10, margins=(14, 12, 14, 12))
        if title:
            lay.addWidget(label(title, "CardHead"))
        if text:
            lay.addWidget(label(esc(text), wrap=True, rich=True))
        for e in edits:
            box = label(diff_html(e["path"], e.get("line", 1), e["old"], e["new"]), rich=True)
            box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            box.setMinimumWidth(0)
            code = frame("Console", vbox(box, margins=(12, 10, 12, 10)))
            lay.addWidget(code)
            apply_b = primary("Apply edit", "check", lambda e=e: self.apply(e))
            reject = Btn("Reject", "x", on=lambda e=e: self.reject(e))
            note = label("exact-text edit · undoable", "Faint", wrap=True)
            note.setStyleSheet("font-size: 11.5px;")
            e["_row"] = row = frame("", hbox(apply_b, reject, 8, note, 1, spacing=8))
            lay.addWidget(row)
        self.setLayout(lay)
        if edits:
            page.preview(edits[0])

    def apply(self, e: dict[str, Any]) -> None:
        ok = self.page.apply_edit(e)
        if ok:
            self._done(e, "Applied. Ctrl Z in the editor undoes it.", "ok")

    def reject(self, e: dict[str, Any]) -> None:
        self.page.editor.clear_proposal()
        self._done(e, "Rejected. Nothing changed.", "muted")

    def _done(self, e: dict[str, Any], text: str, tone: str) -> None:
        row: QFrame = e["_row"]
        lay = row.layout()
        clear(lay)
        if lay is not None:
            lay.addWidget(Icon("check" if tone == "ok" else "x", tone, 14))  # type: ignore[union-attr]
            lay.addWidget(label(text, "Small"))  # type: ignore[union-attr]
            lay.addStretch(1)  # type: ignore[union-attr]

class AiPane(QFrame):
    def __init__(self, page: EditorPage) -> None:
        super().__init__()
        self.setObjectName("AiPane")
        self.page = page
        self.helper: dict[str, Any] | None = None
        self.badge = Chip("official · jailed", "", "shield")
        info = InfoBtn(
            "AI assistant",
            "A plugin answers questions about this service. Pythonator decides what it sees: the open file (secrets "
            "masked), ruff's problems and, if you granted it, the last 40 log lines, masked. It has no files and no "
            "network of its own; its one request goes through Pythonator to the host you granted. It can only "
            "propose exact-text edits, which you apply or reject.",
            rows=[("Sees", "open file, problems, masked logs"), ("Network", "granted hosts only, via Pythonator"),
                  ("Can change", "nothing; it proposes")],
            ico="sparkle",
        )
        close = Btn(ico="x", kind="ghost", size="sm", tip="Hide the assistant (the sparkle above the editor brings it back)",
                    on=lambda: page.show_ai(False))
        head = hbox(Icon("sparkle", "accent", 16), label("AI assistant", "H3"), 1, self.badge, info, close, spacing=8,
                    margins=(18, 14, 10, 14))
        hw = frame("Row", head)
        self.items = QVBoxLayout()
        self.items.setSpacing(12)
        self.items.setContentsMargins(18, 16, 18, 16)
        inner = QWidget()
        iv = vbox(spacing=0)
        iv.addLayout(self.items)
        iv.addStretch(1)
        inner.setLayout(iv)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(inner)
        self.ask = QLineEdit()
        self.ask.setObjectName("Bare")
        self.ask.setPlaceholderText("Ask about this service…")
        self.ask.returnPressed.connect(self.send)
        self.ask_icon = Icon("sparkle", "faint", 14)
        ab = hbox(self.ask_icon, self.ask, Kbd("Enter"), spacing=8, margins=(12, 4, 8, 4))
        ab.setStretch(1, 1)
        box = frame("Search", ab)
        box.setFixedHeight(42)
        self.setLayout(vbox(hw, self.scroll, frame("", vbox(box, margins=(18, 10, 18, 16))), spacing=0))
        self.empty()

    def empty(self) -> None:
        self._clear()
        n = Note("Turn on the <b>AI assistant</b> plugin to ask questions here. It is official and runs jailed; "
                 "you grant it the one host it may reach and set its key in Plugins.", "info", "sparkle")
        self.items.addWidget(n)
        self.items.addWidget(hbox_w(Btn("Open Plugins", "plug", on=lambda: self.page.main.go("plugins")), 1))

    def set_helpers(self, helpers: list[dict[str, Any]]) -> None:
        self.helper = next((h for h in helpers if h.get("state") == "running"), helpers[0] if helpers else None)
        self.ask.setEnabled(self.helper is not None)
        if self.helper is None:
            self.empty()
            self.badge.hide()
        else:
            self.badge.set("official · jailed" if self.helper.get("official") else "jailed", "", "shield")
            self.badge.show()
            if not self.items.count() or isinstance(self.items.itemAt(0).widget(), Note):  # type: ignore[union-attr]
                self._clear()

    def _clear(self) -> None:
        clear(self.items)

    def bubble(self, text: str) -> None:
        q = label(esc(text), wrap=True, rich=True)
        b = frame("Bubble", vbox(q, margins=(14, 10, 14, 10)))
        t = th.current()
        b.setStyleSheet(f"#Bubble {{ background: {t.raised}; border: 1px solid {th.rgba(t.accent, 0.45)}; "
                        f"border-radius: {th.RCARD}px; }}")
        row = hbox(28, b, spacing=0)
        row.setStretch(1, 1)
        self.items.addLayout(row)

    def sent_note(self, text: str) -> None:
        t = th.current()
        lb = label(esc(text), "Small", wrap=True, rich=True)
        row = hbox(Icon("eye", "muted", 14), lb, spacing=10, margins=(14, 10, 14, 10))
        row.setStretch(1, 1)
        f = frame("Sent", row)
        f.setStyleSheet(f"#Sent {{ border: 1px dashed {t.border_strong}; border-radius: {th.RCARD}px; }}")
        self.items.addWidget(f)

    def send(self) -> None:
        q = self.ask.text().strip()
        if not q or self.helper is None:
            return
        self.ask.clear()
        self.bubble(q)
        wait = label("Asking…", "Muted")
        self.items.addWidget(wait)
        pg = self.page
        tab = pg.cur()

        def got(res: Any, err: Any) -> None:
            wait.deleteLater()
            if err:
                self.items.addWidget(Note(esc(err["message"]) + (f"<br>{esc(err['fix'])}" if err.get("fix") else ""),
                                          "bad", "warn"))
                return
            self.show_answer(res)

        pg.main.link.call("assist.ask", got, name=pg.name, plugin=self.helper["id"], path=tab["path"] if tab else "",
                          text=pg.editor.text() if tab else None, problems=tab.get("problems", []) if tab else [],
                          question=q)

    def show_answer(self, res: dict[str, Any]) -> None:
        self.items.addWidget(Proposal(self.page, str(res.get("text", "")), list(res.get("edits") or [])))
        if res.get("sent"):
            self.sent_note(str(res["sent"]))
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(0))  # type: ignore[union-attr]

def hbox_w(*items: Any) -> QWidget:
    return frame("", hbox(*items))

class EditorPage(QWidget):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.name = ""
        self.spec: dict[str, Any] = {}
        self.tabs_data: list[dict[str, Any]] = []
        self.sessions: dict[str, tuple[list[dict[str, Any]], int]] = {}
        self.changed: list[str] = []
        self.helpers: list[dict[str, Any]] = []
        self._opening: set[str] = set()
        self._lint_timer = QTimer(self, singleShot=True, interval=500, timeout=self.lint)

        # left: files --------------------------------------------------------------------------------------------
        self.tree_frame = frame("Tree")
        self.svc_btn = Btn("Files", "down", "ghost", trailing=True, tip="Show another service's files", on=self._pick_service)
        self.svc_btn.setStyleSheet("font-size: 14px; font-weight: 700; padding: 0 6px;")
        self.git_chip = Chip("", "", "git")
        self.git_chip.setToolTip("Private snapshots of this folder (Settings → Git). Files changed since the last one.")
        self.tree = FileTree()
        self.tree.setHeaderHidden(True)
        self.open_dirs: set[str] = set()
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(15, 15))
        self.tree.itemClicked.connect(self._tree_clicked)
        self.env_note = Note(".env values are masked here and in every log, plugin and AI request.", "info", "lock")
        tl = vbox(hbox(self.svc_btn, 1, self.git_chip, spacing=6), self.tree, self.env_note, spacing=10, margins=(12, 12, 12, 12))
        self.tree_frame.setLayout(tl)

        # middle: tabs, editor, problems -----------------------------------------------------------------------------
        self.tabbar = QTabBar()
        self.tabbar.setObjectName("FileTabs")
        self.tabbar.setTabsClosable(False)
        self.tabbar.setExpanding(False)
        self.tabbar.setDrawBase(False)
        self.tabbar.setIconSize(QSize(14, 14))
        self.tabbar.setMovable(True)  # drag to reorder; the list behind the tabs follows
        self.tabbar.tabMoved.connect(lambda a, b: self.tabs_data.insert(b, self.tabs_data.pop(a)))
        self.tabbar.currentChanged.connect(self._tab_changed)
        self.lint_level = str(QSettings("Pythonator", "Pythonator").value("editor/lint", "errors"))
        self.lint_chip = Btn("ruff", "shield", "ghost", "sm", on=self._lint_menu,
                             tip="Problems come from ruff, run by the background service in a jail with no folder and no "
                                 "network: it only sees the text of this file. Click to choose how much it checks.")
        self.rt_chip = Chip("", "", "")
        self.ai_btn = Btn(ico="sparkle", kind="ghost", size="sm", tip="Show the AI assistant", on=lambda: self.show_ai(True))
        tabrow = frame("TabRow", hbox(self.tabbar, 1, self.lint_chip, self.rt_chip, self.ai_btn, spacing=8,
                                      margins=(0, 0, 12, 0)))
        tabrow.setFixedHeight(46)
        self.editor = Editor()
        self.editor.textChanged.connect(self._edited)
        self.editor.cursorPositionChanged.connect(lambda _l, _c: self._hover())
        self.pop = frame("Pop")
        self.pop_text = label("", rich=True)
        self.pop.setLayout(hbox(Icon("warn", "warn", 14), self.pop_text, Kbd("Ctrl ."), spacing=8, margins=(10, 6, 8, 6)))
        self.pop.setParent(self.editor)
        self.pop.hide()
        self.blank = frame("", vbox(1, label("Pick a file on the left.", "Muted"), 1, margins=(30, 30, 30, 30)))
        self.prob_head = label("", rich=True)
        self.prob_src = label("from ruff (jailed, read-only)", "Small")
        self.prob_rows = QVBoxLayout()
        self.prob_rows.setSpacing(0)
        self.prob_open, self.prob_list = False, []  # one problem at first; all of them (scrolled) when opened
        self.prob_more = Btn("Show more", "down", "ghost", "sm", on=lambda: self._problems(self.prob_list, not self.prob_open))
        self.prob_scroll = QScrollArea()
        self.prob_scroll.setWidgetResizable(True)
        self.prob_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.prob_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.prob_scroll.setWidget(frame("", self.prob_rows))
        self.probs = probs = frame("Problems", vbox(frame("Row", hbox(Icon("warn", "warn", 15), self.prob_head, 1, self.prob_src,
                                                         self.prob_more, spacing=8, margins=(18, 10, 18, 10))),
                                       self.prob_scroll, spacing=0))
        mid = QWidget()
        self.mid_lay = vbox(tabrow, self.editor, self.blank, probs, spacing=0)
        self.mid_lay.setStretchFactor(self.editor, 1)  # the problems take only what they need
        mid.setLayout(self.mid_lay)

        # right: assistant --------------------------------------------------------------------------------------------
        self.ai = AiPane(self)
        self.ai_on = QSettings("Pythonator", "Pythonator").value("editor/ai", "off") == "on"  # optional, off at first
        self.split = splitter("editor", (self.tree_frame, 290, 180, 560), (mid, 900, 320, 16777215), (self.ai, 420, 300, 760))
        self.setLayout(hbox(self.split, spacing=0))

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
        QShortcut(QKeySequence("Ctrl+."), self, activated=self.quick_fix_here)
        self._show_editor(False)
        self._lint_label()
        self.retheme()

    # framing --------------------------------------------------------------------------------------------------------
    def retheme(self) -> None:
        t = th.current()
        self.editor.retheme()
        self.setStyleSheet(
            f"#TabRow {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}"
            f"#FileTabs::tab {{ background: transparent; color: {t.muted}; border: 0; border-right: 1px solid {t.border};"
            f" border-top: 2px solid transparent; padding: 0 14px; min-height: 44px; font-size: 13px; }}"
            f"#FileTabs::tab:selected {{ color: {t.text}; background: {t.bg}; border-top: 2px solid {t.accent}; }}"
            f"#Problems {{ background: {t.surface}; border-top: 1px solid {t.border}; }}"
            f"#Tree QTreeView {{ show-decoration-selected: 0; }} #Tree QTreeView::branch {{ background: transparent; }}"
        )
        for i in range(self.tabbar.count()):
            d = self.tabs_data[i]
            self.tabbar.setTabIcon(i, icon("code" if d["path"].endswith(CODE_EXT) else "file", t.muted, 14))
        self._fill_tree()

    def layout_for(self, w: int) -> None:
        self._width = w
        show_if(self.ai, w >= 1180 and self.ai_on)
        show_if(self.ai_btn, w >= 1180 and not self.ai_on)
        show_if(self.tree_frame, w >= 760)

    def show_ai(self, on: bool) -> None:
        self.ai_on = on
        QSettings("Pythonator", "Pythonator").setValue("editor/ai", "on" if on else "off")
        self.layout_for(getattr(self, "_width", self.window().width()))

    def refresh(self) -> None:
        if not self.name and self.main.current:
            self.open_service(self.main.current)
        self.main.link.call("assist.list", self._got_helpers)

    def _got_helpers(self, res: Any, err: Any) -> None:
        if not err and isinstance(res, list):
            self.helpers = res
            self.ai.set_helpers(res)

    # services and files ---------------------------------------------------------------------------------------------
    def open_service(self, name: str, path: str | None = None) -> None:
        if not name:
            return
        s = self.main._svc(name)
        if s is None:
            return
        if name != self.name:  # each service's tabs (with their undo) are kept while the window runs
            self.sessions[self.name] = (self.tabs_data, self.tabbar.currentIndex())
            self.tabs_data = []
            while self.tabbar.count():
                self.tabbar.removeTab(0)
            self._show_editor(False)
            tabs, at = self.sessions.pop(name, ([], -1))
            for d in tabs:
                self._add_tab(d)
            self.tabbar.setCurrentIndex(at)
        self.name = name
        self.spec = s["spec"]
        self.svc_btn.setText(f"Files · {name}")
        rt = RUNTIMES.get(self.spec.get("runtime", "python"))
        ver = self.spec.get("runtime_version", "")
        label_ = (rt.label if rt else self.spec.get("runtime", "")) + (f" {ver}" if ver else "")
        self.rt_chip.set(label_ + (" · uv" if self.spec.get("runtime") == "python" and ver else ""), "", "")
        self.main.link.call("files.list", lambda r, e: self._got_tree(r, e, path), name=name)
        self.main.link.call("files.names", lambda r, e: None if e else self.editor.use_words(r), name=name)
        self.main.link.call("assist.list", self._got_helpers)

    def _pick_service(self) -> None:
        m = QMenu(self)
        for s in self.main.services:
            n = s["spec"]["name"]
            m.addAction(n, lambda n=n: self.open_service(n))
        m.exec(self.svc_btn.mapToGlobal(QPoint(0, self.svc_btn.height())))

    def _got_tree(self, res: Any, err: Any, path: str | None) -> None:
        if err:
            QMessageBox.warning(self, "Files", err["message"])
            return
        self.entries = res["tree"]
        self.changed = list(res.get("changed") or [])
        self.git_chip.set(f"snapshot · {len(self.changed)} changed" if self.changed else "snapshot · clean", "", "git")
        self._fill_tree()
        first = path or next((e["path"] for e in self.entries if e["kind"] == "file" and e["path"] in (
            "main.py", "app.py", "index.js", "main.go", "server.js")), None)
        if first and not self.tabs_data:
            self.open_file(first)

    def _fill_tree(self) -> None:
        t = th.current()
        self.tree.clear()
        counts = {d["path"]: len(d.get("problems", [])) for d in self.tabs_data}
        cur = self.cur()
        root = QTreeWidgetItem([self.spec.get("folder", "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or self.name])
        root.setIcon(0, icon("folder", t.muted, 15))
        self.tree.addTopLevelItem(root)
        parents: dict[str, QTreeWidgetItem] = {"": root}
        for e in getattr(self, "entries", []):
            parent = parents.get(e["path"].rsplit("/", 1)[0] if "/" in e["path"] else "", root)
            it = QTreeWidgetItem(parent, [e["path"].rsplit("/", 1)[-1]])
            it.setData(0, Qt.ItemDataRole.UserRole, e["path"])
            it.setIcon(0, icon("folder" if e["kind"] == "dir" else "file", t.muted, 15))
            if e["kind"] == "dir":
                parents[e["path"]] = it
                it.setExpanded(e["path"] in self.open_dirs)
            right = None
            if e.get("masked"):
                right = Chip("masked", "warn", h=20)
            elif counts.get(e["path"]):
                right = Chip(str(counts[e["path"]]), "warn", h=20)
            elif e["path"] in self.changed:
                right = Chip("M", "info", h=20)
            if right is not None:
                self.tree.setItemWidget(it, 0, frame("", hbox(1, right, margins=(0, 0, 6, 0))))
            if cur and e["path"] == cur["path"]:
                self.tree.setCurrentItem(it)
                p = it.parent()
                while p is not None:  # the open file's folders stay open
                    p.setExpanded(True)
                    p = p.parent()
        root.setExpanded(True)

    def _tree_clicked(self, it: QTreeWidgetItem, _col: int) -> None:
        path = it.data(0, Qt.ItemDataRole.UserRole)
        kind = next((e["kind"] for e in getattr(self, "entries", []) if e["path"] == path), "")
        if path and kind == "file":
            self.open_file(path)
        elif path:  # a folder opens and closes with one click, and stays that way when the tree refreshes
            it.setExpanded(not it.isExpanded())
            (self.open_dirs.add if it.isExpanded() else self.open_dirs.discard)(path)

    def open_file(self, path: str) -> None:
        for i, d in enumerate(self.tabs_data):
            if d["path"] == path:
                self.tabbar.setCurrentIndex(i)
                return

        if path in self._opening:
            return
        self._opening.add(path)

        def got(res: Any, err: Any) -> None:
            self._opening.discard(path)
            if err:
                QMessageBox.information(self, path, err["message"] + (f"\n\n{err['fix']}" if err.get("fix") else ""))
                return
            if any(x["path"] == res["path"] for x in self.tabs_data):
                return
            self.tabbar.setCurrentIndex(self._add_tab({
                "path": res["path"], "text": res["text"], "hash": res["hash"], "readonly": res["readonly"],
                "masked": res["masked"], "problems": [], "dirty": False}))

        self.main.link.call("files.read", got, name=self.name, path=path)

    def _add_tab(self, d: dict[str, Any]) -> int:
        self.tabs_data.append(d)
        name = d["path"].rsplit("/", 1)[-1] + (" •" if d["dirty"] else "")
        i = self.tabbar.addTab(icon("code" if d["path"].endswith(CODE_EXT) else "file", th.current().muted, 14), name)
        x = Btn("", "x", "ghost", "sm", tip="Close")
        x.clicked.connect(lambda _=False, d=d: self.close_tab(self.tabs_data.index(d)) if d in self.tabs_data else None)
        self.tabbar.setTabButton(i, QTabBar.ButtonPosition.RightSide, x)
        return i

    def cur(self) -> dict[str, Any] | None:
        i = self.tabbar.currentIndex()
        return self.tabs_data[i] if 0 <= i < len(self.tabs_data) else None

    def _show_editor(self, on: bool) -> None:
        show_if(self.editor, on)
        show_if(self.blank, not on)

    def _tab_changed(self, i: int) -> None:
        prev = getattr(self, "_shown", None)
        if prev is not None and prev in self.tabs_data:
            prev["text"] = self.editor.text()
        d = self.cur()
        self._shown = d
        if d is None:
            self._show_editor(False)
            self._problems([])
            return
        self._loading = True
        self.editor.set_file(d["path"], d.get("doc") or d["text"], d["readonly"])
        d["doc"] = self.editor.document()
        self._loading = False
        self._show_editor(True)
        self._problems(d["problems"])
        self.editor.mark_problems(d["problems"])
        self._fill_tree()
        self.lint()

    def close_tab(self, i: int) -> None:
        d = self.tabs_data[i]
        if d["dirty"]:
            r = QMessageBox.question(self, d["path"], "Save changes before closing?",
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                     | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            if r == QMessageBox.StandardButton.Save:
                self.tabbar.setCurrentIndex(i)
                self.save()
        self._shown = None
        self.tabs_data.pop(i)
        self.tabbar.removeTab(i)

    def _edited(self) -> None:
        if getattr(self, "_loading", False):
            return
        d = self.cur()
        if d is None:
            return
        if not d["dirty"]:
            d["dirty"] = True
            self.tabbar.setTabText(self.tabbar.currentIndex(), d["path"].rsplit("/", 1)[-1] + " •")
        self._lint_timer.start()

    def save(self) -> None:
        d = self.cur()
        if d is None or d["readonly"]:
            return
        text = self.editor.text()
        i = self.tabbar.currentIndex()

        def got(res: Any, err: Any) -> None:
            if err:
                QMessageBox.warning(self, d["path"], err["message"] + (f"\n\n{err['fix']}" if err.get("fix") else ""))
                return
            d.update(hash=res["hash"], text=text, dirty=False)
            self.tabbar.setTabText(i, d["path"].rsplit("/", 1)[-1])
            self.main.link.call("files.list", lambda r, e: self._got_tree(r, e, None), name=self.name)

        self.main.link.call("files.write", got, name=self.name, path=d["path"], text=text, hash=d["hash"])

    # problems -------------------------------------------------------------------------------------------------------
    LEVELS = (("off", "Problems off"), ("errors", "Errors only (default)"), ("all", "All ruff rules (the project's own)"))

    def _lint_menu(self) -> None:
        from PyQt6.QtWidgets import QMenu

        m = QMenu(self)
        for key, text in self.LEVELS:
            a = m.addAction(text)
            a.setCheckable(True)
            a.setChecked(key == self.lint_level)
            a.triggered.connect(lambda _=False, k=key: self.set_lint_level(k))
        m.exec(self.lint_chip.mapToGlobal(self.lint_chip.rect().bottomLeft()))

    def set_lint_level(self, key: str) -> None:
        self.lint_level = key
        QSettings("Pythonator", "Pythonator").setValue("editor/lint", key)
        self._lint_label()
        self.lint()

    def _lint_label(self) -> None:
        self.lint_chip.setText({"off": "ruff · off", "errors": "ruff · errors", "all": "ruff · all rules"}.get(
            self.lint_level, "ruff"))
        self.probs.hide()  # shown again when there is a problem to list

    def lint(self) -> None:
        d = self.cur()
        if d is not None and self.lint_level == "off":
            d["problems"] = []
            self.editor.mark_problems([])
            return
        if d is None or not d["path"].endswith((".py", ".pyi")):
            if d is not None:
                self._problems([])
            return
        text = self.editor.text()

        def got(res: Any, err: Any) -> None:
            if d is not self.cur():
                return
            if err:
                self.prob_src.setText(f"ruff: {err['message']}")
                return
            self.prob_src.setText("from ruff (jailed, read-only)")
            d["problems"] = res
            self._problems(res)
            self.editor.mark_problems(res)
            self._fill_tree()
            self._hover()

        self.main.link.call("lint.check", got, name=self.name, path=d["path"], text=text, level=self.lint_level)

    def _problems(self, probs: list[dict[str, Any]], opened: bool | None = None) -> None:
        show_if(self.probs, bool(probs) and self.lint_level != "off")  # no empty "0 problems" bar
        self.prob_list, self.prob_open = probs, self.prob_open if opened is None else opened
        show_if(self.prob_more, len(probs) > 1)
        self.prob_more.setText("Show one" if self.prob_open else f"Show {len(probs) - 1} more")
        self.prob_more.set_icon("up" if self.prob_open else "down")
        t = th.current()
        clear(self.prob_rows)
        errs = sum(1 for p in probs if p["severity"] == "error")
        warns = len(probs) - errs
        self.prob_head.setText(f"<b>Problems</b> · {warns} warning{'s' if warns != 1 else ''} · {errs} error"
                               f"{'s' if errs != 1 else ''}")
        d = self.cur()
        for p in probs[:200 if self.prob_open else 1]:
            txt = label(f"<span style='font-family:\"{th.MONO_FAMILY}\";font-size:12.5px'>{esc(d['path'] if d else '')}:"
                        f"{p['line']} · <span style='color:{t.muted}'>{esc(p['code'])}</span> {esc(p['message'])}</span>",
                        rich=True)
            row = hbox(Icon("warn", "bad" if p["severity"] == "error" else "warn", 15), txt, 1, spacing=10,
                       margins=(18, 7, 18, 7))
            if p["fixable"]:
                row.addWidget(Btn("Quick fix (proposal)", size="sm", on=lambda p=p: self.quick_fix(p),
                                  tip=p.get("fix") or "Ruff's fix, shown as a proposal"))
            else:
                row.addWidget(Btn("Go to line", size="sm", on=lambda p=p: self._goto(p)))
            self.prob_rows.addWidget(frame("", row))
        # once the rows are styled: as tall as they are, up to 260 px, then scrolled
        QTimer.singleShot(0, lambda: self.prob_scroll.setFixedHeight(min(260, self.prob_rows.sizeHint().height())))

    def _goto(self, p: dict[str, Any]) -> None:
        self.editor.setCursorPosition(p["line"] - 1, p["col"] - 1)
        self.editor.ensureLineVisible(p["line"] - 1)
        self.editor.setFocus()

    def _at_cursor(self) -> dict[str, Any] | None:
        d = self.cur()
        if d is None:
            return None
        line, col = self.editor.getCursorPosition()
        for p in d["problems"]:
            if p["line"] - 1 <= line <= p["end_line"] - 1 and (p["line"] != p["end_line"] or p["col"] - 1 <= col <= p["end_col"]):
                return p
        return None

    def _hover(self) -> None:
        p = self._at_cursor()
        if p is None:
            self.pop.hide()
            return
        t = th.current()
        self.pop_text.setText(f"<span style='font-family:\"{th.MONO_FAMILY}\";font-size:12.5px'>ruff {esc(p['code'])} · "
                              f"<b>{esc(p['message'])}</b></span>")
        self.pop.setStyleSheet(f"#Pop {{ background: {t.console}; border: 1px solid {t.border_strong}; border-radius: 6px; }}")
        self.pop.adjustSize()
        line, _c = p["line"] - 1, p["col"] - 1
        pos = self.editor.positionFromLineIndex(line, max(0, p["col"] - 1))
        x = self.editor.SendScintilla(QsciScintilla.SCI_POINTXFROMPOSITION, 0, pos)
        y = self.editor.SendScintilla(QsciScintilla.SCI_POINTYFROMPOSITION, 0, pos)
        h = self.editor.SendScintilla(QsciScintilla.SCI_TEXTHEIGHT, line)
        self.pop.move(max(8, min(x + 40, self.editor.width() - self.pop.width() - 16)), y + h + 2)
        self.pop.show()
        self.pop.raise_()

    def quick_fix_here(self) -> None:
        p = self._at_cursor()
        if p and p["fixable"]:
            self.quick_fix(p)

    def quick_fix(self, p: dict[str, Any]) -> None:
        d = self.cur()
        if d is None:
            return
        before = self.editor.text()

        def got(res: Any, err: Any) -> None:
            if err:
                QMessageBox.warning(self, "Quick fix", err["message"])
                return
            after = res["text"]
            edit = one_edit(before, after)
            if edit is None:
                self.ai.items.addWidget(Note("Ruff had no change to propose for this one.", "info"))
                return
            edit.update(path=d["path"])
            self.ai.items.addWidget(Proposal(self, "", [edit], f"Quick fix · ruff {p['code']} · {p.get('fix', '')}"))

        self.main.link.call("lint.fix", got, name=self.name, path=d["path"], text=before, code=p["code"])

    # proposals -----------------------------------------------------------------------------------------------------
    def preview(self, e: dict[str, Any]) -> None:
        d = self.cur()
        if d is None or d["path"] != e["path"]:
            return
        line = self.editor.show_proposal(e["old"], e["new"])
        if line >= 0:
            self.editor.setFirstVisibleLine(max(0, line - 12))

    def apply_edit(self, e: dict[str, Any]) -> bool:
        d = self.cur()
        if d is None or d["path"] != e["path"]:
            self.open_file(e["path"])
            QMessageBox.information(self, "Apply edit", "Opened the file; press Apply edit again.")
            return False
        self.editor.clear_proposal()
        if not self.editor.replace_once(e["old"], e["new"]):
            QMessageBox.warning(self, "Apply edit", "The text this edit replaces changed, so it wasn't applied. Ask again.")
            return False
        self.save()
        return True

def one_edit(before: str, after: str) -> dict[str, Any] | None:
    """The smallest exact-text edit that turns before into after (whole lines, plus a line of context)."""
    if before == after:
        return None
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    i1, i2, j1, j2 = ops[0][1], ops[-1][2], ops[0][3], ops[-1][4]
    while True:
        old = "".join(a[i1:i2])
        if old and before.count(old) == 1:
            break
        if i1 == 0 and i2 >= len(a):
            old = before
            break
        if i1 > 0:
            i1, j1 = i1 - 1, j1 - 1
        if i2 < len(a):
            i2, j2 = i2 + 1, j2 + 1
    return {"old": old, "new": "".join(b[j1:j2]), "line": i1 + 1}
