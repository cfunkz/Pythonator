"""Python syntax highlighting for the code editor."""

from __future__ import annotations

import builtins
import keyword
import re

from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.DemiBold)
    if italic:
        f.setFontItalic(True)
    return f


_TOKEN = re.compile(
    r"""
    (?P<comment>\#.*)
  | (?P<tstr>(?<!\w)[rRbBuUfF]{0,2}(?:'''|\"\"\"))
  | (?P<str>(?<!\w)[rRbBuUfF]{0,2}(?:'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?))
  | (?P<deco>^\s*@[A-Za-z_][\w.]*)
  | (?P<num>\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?j?)\b)
  | (?P<ident>[A-Za-z_]\w*)
    """,
    re.VERBOSE,
)
_CALL = re.compile(r"\s*\(")

KEYWORDS = frozenset(keyword.kwlist) | frozenset(getattr(keyword, "softkwlist", []))
BUILTINS = frozenset(n for n in dir(builtins) if not n.startswith("_"))
_CONSTANTS = frozenset({"True", "False", "None", "Ellipsis", "NotImplemented"})
_SELF = frozenset({"self", "cls"})

STATE_NONE, STATE_SQ3, STATE_DQ3 = 0, 1, 2


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document: QTextDocument | None) -> None:
        super().__init__(document)
        self.f_kw = _fmt("#c792ea", bold=True)
        self.f_const = _fmt("#f78c6c", bold=True)
        self.f_builtin = _fmt("#82aaff")
        self.f_self = _fmt("#f07178", italic=True)
        self.f_def = _fmt("#82aaff", bold=True)
        self.f_class = _fmt("#ffcb6b", bold=True)
        self.f_call = _fmt("#89ddff")
        self.f_deco = _fmt("#ffcb6b")
        self.f_num = _fmt("#f78c6c")
        self.f_str = _fmt("#c3e88d")
        self.f_comment = _fmt("#697386", italic=True)
        self.f_upper = _fmt("#ff9e64")

    def highlightBlock(self, text: str | None) -> None:
        if text is None:
            return
        pos = 0
        state = self.previousBlockState()
        if state in (STATE_SQ3, STATE_DQ3):
            end = self._close_triple(text, 0, "'''" if state == STATE_SQ3 else '"""')
            if end < 0:
                self.setFormat(0, len(text), self.f_str)
                self.setCurrentBlockState(state)
                return
            self.setFormat(0, end, self.f_str)
            pos = end
        self.setCurrentBlockState(STATE_NONE)

        pending: str | None = None  # "def" / "class": colour the next identifier as a name
        while (m := _TOKEN.search(text, pos)) is not None:
            kind = m.lastgroup
            start, end = m.start(), m.end()
            if kind == "tstr":
                delim = m.group()[-3:]
                close = self._close_triple(text, end, delim)
                if close < 0:
                    self.setFormat(start, len(text) - start, self.f_str)
                    self.setCurrentBlockState(STATE_SQ3 if delim == "'''" else STATE_DQ3)
                    return
                self.setFormat(start, close - start, self.f_str)
                pos = close
                continue
            if kind == "comment":
                self.setFormat(start, end - start, self.f_comment)
            elif kind == "str":
                self.setFormat(start, end - start, self.f_str)
            elif kind == "deco":
                at = text.index("@", start)
                self.setFormat(at, end - at, self.f_deco)
            elif kind == "num":
                self.setFormat(start, end - start, self.f_num)
            elif kind == "ident":
                word = m.group()
                fmt: QTextCharFormat | None = None
                if pending:
                    fmt = self.f_def if pending == "def" else self.f_class
                    pending = None
                elif word in _CONSTANTS:
                    fmt = self.f_const
                elif word in KEYWORDS:
                    fmt = self.f_kw
                    if word in ("def", "class"):
                        pending = word
                elif word in _SELF:
                    fmt = self.f_self
                elif _CALL.match(text, end):
                    fmt = self.f_builtin if word in BUILTINS else self.f_call
                elif word in BUILTINS:
                    fmt = self.f_builtin
                elif len(word) > 1 and word.isupper():
                    fmt = self.f_upper
                if fmt is not None:
                    self.setFormat(start, end - start, fmt)
            pos = max(end, start + 1)

    @staticmethod
    def _close_triple(text: str, start: int, delim: str) -> int:
        """Index just past the closing ``delim`` at/after ``start`` (skipping escapes), or -1."""
        i = start
        while (j := text.find(delim, i)) >= 0:
            backslashes = 0
            k = j - 1
            while k >= start and text[k] == "\\":
                backslashes += 1
                k -= 1
            if backslashes % 2 == 0:
                return j + 3
            i = j + 1
        return -1


_JS_TOKEN = re.compile(
    r"""
    (?P<comment>//.*)
  | (?P<block>/\*)
  | (?P<tpl>`)
  | (?P<str>'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?)
  | (?P<deco>@[A-Za-z_$][\w$.]*)
  | (?P<num>\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?n?)\b)
  | (?P<ident>[A-Za-z_$][\w$]*)
    """,
    re.VERBOSE,
)

JS_KEYWORDS = frozenset(
    "async await break case catch class const continue debugger default delete do else export extends "
    "finally for from function if import in instanceof let new of return static super switch this "
    "throw try typeof var void while with yield as type interface enum implements private protected "
    "public readonly declare namespace abstract satisfies keyof".split()
)
JS_CONSTANTS = frozenset({"true", "false", "null", "undefined", "NaN", "Infinity"})
JS_BUILTINS = frozenset(
    "console process require module exports globalThis window document Promise JSON Math Object Array "
    "String Number Boolean Symbol BigInt Map Set WeakMap WeakSet Date RegExp Error TypeError Buffer "
    "setTimeout setInterval clearTimeout clearInterval fetch URL __dirname __filename".split()
)

JS_IN_BLOCK, JS_IN_TEMPLATE = 3, 4


class JsHighlighter(PythonHighlighter):
    """JavaScript / TypeScript highlighting (shares the Python palette)."""

    def highlightBlock(self, text: str | None) -> None:
        if text is None:
            return
        pos = 0
        state = self.previousBlockState()
        if state == JS_IN_BLOCK:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self.f_comment)
                self.setCurrentBlockState(JS_IN_BLOCK)
                return
            self.setFormat(0, end + 2, self.f_comment)
            pos = end + 2
        elif state == JS_IN_TEMPLATE:
            end = self._close_template(text, 0)
            if end < 0:
                self.setFormat(0, len(text), self.f_str)
                self.setCurrentBlockState(JS_IN_TEMPLATE)
                return
            self.setFormat(0, end, self.f_str)
            pos = end
        self.setCurrentBlockState(STATE_NONE)

        pending = False  # colour the identifier after ``function`` / ``class``
        while (m := _JS_TOKEN.search(text, pos)) is not None:
            kind, start, end = m.lastgroup, m.start(), m.end()
            if kind == "block":
                close = text.find("*/", end)
                if close < 0:
                    self.setFormat(start, len(text) - start, self.f_comment)
                    self.setCurrentBlockState(JS_IN_BLOCK)
                    return
                self.setFormat(start, close + 2 - start, self.f_comment)
                pos = close + 2
                continue
            if kind == "tpl":
                close = self._close_template(text, end)
                if close < 0:
                    self.setFormat(start, len(text) - start, self.f_str)
                    self.setCurrentBlockState(JS_IN_TEMPLATE)
                    return
                self.setFormat(start, close - start, self.f_str)
                pos = close
                continue
            if kind == "comment":
                self.setFormat(start, end - start, self.f_comment)
            elif kind == "str":
                self.setFormat(start, end - start, self.f_str)
            elif kind == "deco":
                self.setFormat(start, end - start, self.f_deco)
            elif kind == "num":
                self.setFormat(start, end - start, self.f_num)
            elif kind == "ident":
                word = m.group()
                fmt: QTextCharFormat | None = None
                if pending:
                    fmt, pending = self.f_def, False
                elif word in JS_CONSTANTS:
                    fmt = self.f_const
                elif word in JS_KEYWORDS:
                    fmt = self.f_self if word == "this" else self.f_kw
                    pending = word in ("function", "class")
                elif _CALL.match(text, end):
                    fmt = self.f_builtin if word in JS_BUILTINS else self.f_call
                elif word in JS_BUILTINS:
                    fmt = self.f_builtin
                elif len(word) > 1 and word.isupper():
                    fmt = self.f_upper
                if fmt is not None:
                    self.setFormat(start, end - start, fmt)
            pos = max(end, start + 1)

    @staticmethod
    def _close_template(text: str, start: int) -> int:
        """Index just past the closing backtick at/after ``start``, or -1 (``${}`` stays string-coloured)."""
        i = start
        while i < len(text):
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                return i + 1
            i += 1
        return -1


def language_for(path: str | None) -> str:
    """'python', 'javascript', another language from :mod:`.languages`, or 'text', by extension."""
    from .languages import BY_EXTENSION  # noqa: PLC0415 - languages builds on this module

    if not path:
        return "python"
    lower = path.lower()
    if lower.endswith((".py", ".pyw", ".pyi")):
        return "python"
    if lower.endswith((".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")):
        return "javascript"
    dot = lower.rfind(".")
    spec = BY_EXTENSION.get(lower[dot:]) if dot >= 0 else None
    return spec.name if spec else "text"


def highlighter_for(language: str, document: QTextDocument | None) -> QSyntaxHighlighter | None:
    from .languages import highlighter_for_spec  # noqa: PLC0415

    if language == "python":
        return PythonHighlighter(document)
    if language == "javascript":
        return JsHighlighter(document)
    return highlighter_for_spec(language, document)
