"""Light syntax highlighting for the other languages, driven by small keyword tables.

Python and JavaScript have hand-tuned highlighters in :mod:`.highlighter`; everything else
shares :class:`GenericHighlighter`: comments, strings, numbers, keywords, constants, builtins and
calls, with multi-line block comments and (Go) raw strings. Small and fast on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QTextDocument

from .highlighter import PythonHighlighter


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    keywords: frozenset[str]
    constants: frozenset[str] = frozenset()
    builtins: frozenset[str] = frozenset()
    line_comment: tuple[str, ...] = ("//",)
    block_comment: tuple[str, str] | None = ("/*", "*/")
    raw_string: str | None = None  # a delimiter for strings that may span lines (Go's backtick)
    variables: bool = False  # $name is a variable (shell, PHP, PowerShell)
    case_insensitive: bool = False  # PowerShell keywords
    new_file: str = ""  # template for "New ... file"


GO = LanguageSpec(
    "go", (".go",),
    _words("break case chan const continue default defer else fallthrough for func go goto if import interface "
           "map package range return select struct switch type var"),
    _words("true false nil iota"),
    _words("append cap close complex copy delete imag len make new panic print println real recover any error "
           "string int int64 int32 uint uint64 byte rune bool float64 float32 fmt log os time context http"),
    raw_string="`",
    new_file='package main\n\nimport (\n\t"fmt"\n\t"os"\n\t"os/signal"\n\t"syscall"\n\t"time"\n)\n\n'
             "func main() {\n\tstop := make(chan os.Signal, 1)\n"
             "\tsignal.Notify(stop, os.Interrupt, syscall.SIGTERM)\n"
             "\tticker := time.NewTicker(5 * time.Second)\n\tdefer ticker.Stop()\n"
             '\tfmt.Println("worker started")\n\tfor {\n\t\tselect {\n\t\tcase t := <-ticker.C:\n'
             '\t\t\tfmt.Println("tick", t.Format(time.TimeOnly))\n\t\tcase <-stop:\n'
             '\t\t\tfmt.Println("stopping")\n\t\t\treturn\n\t\t}\n\t}\n}\n',
)
JAVA = LanguageSpec(
    "java", (".java",),
    _words("abstract assert break case catch class continue default do else enum extends final finally for if "
           "implements import instanceof interface native new package private protected public record return "
           "static super switch synchronized this throw throws transient try var void volatile while yield "
           "boolean byte char double float int long short sealed permits"),
    _words("true false null"),
    _words("System String Integer Long Double Boolean Object List Map Set ArrayList HashMap Thread Runtime "
           "Math Optional Exception RuntimeException"),
)
CSHARP = LanguageSpec(
    "csharp", (".cs",),
    _words("abstract as async await base bool break byte case catch char checked class const continue decimal "
           "default delegate do double else enum event explicit extern finally fixed float for foreach get goto "
           "if implicit in init int interface internal is lock long namespace new object operator out override "
           "params private protected public readonly record ref return sealed set short sizeof static string "
           "struct switch this throw try typeof uint ulong using var virtual void volatile while yield"),
    _words("true false null"),
    _words("Console Task String Math DateTime TimeSpan List Dictionary Environment Exception CancellationToken"),
)
RUBY = LanguageSpec(
    "ruby", (".rb", ".rake", ".gemspec"),
    _words("alias and begin break case class def defined? do else elsif end ensure for if in module next not or "
           "redo rescue retry return self super then undef unless until when while yield require require_relative "
           "attr_accessor attr_reader attr_writer private protected public loop lambda proc"),
    _words("true false nil __FILE__ __LINE__"),
    _words("puts print p pp gets sleep raise format sprintf rand Integer Float String Array Hash Time File Dir "
           "Thread Signal STDOUT STDERR ENV ARGV"),
    line_comment=("#",), block_comment=("=begin", "=end"),
    new_file='$stdout.sync = true\n\nrunning = true\n%w[INT TERM].each { |sig| trap(sig) { running = false } }\n\n'
             'puts "worker started"\nwhile running\n  puts "tick #{Time.now.strftime(\'%H:%M:%S\')}"\n'
             '  sleep 5\nend\nputs "stopping"\n',
)
PHP = LanguageSpec(
    "php", (".php",),
    _words("abstract and as break callable case catch class clone const continue declare default do echo else "
           "elseif empty enddeclare endfor endforeach endif endswitch endwhile enum extends final finally fn for "
           "foreach function global goto if implements include include_once instanceof insteadof interface isset "
           "list match namespace new or print private protected public readonly require require_once return "
           "static switch throw trait try unset use var while yield"),
    _words("true false null TRUE FALSE NULL PHP_EOL"),
    _words("array count strlen str_replace sprintf printf json_encode json_decode date time sleep usleep "
           "file_get_contents file_put_contents explode implode in_array array_map var_dump print_r"),
    line_comment=("//", "#"), variables=True,
    new_file='<?php\n\ndeclare(strict_types=1);\n\necho "worker started", PHP_EOL;\n\nwhile (true) {\n'
             '    echo "tick ", date("H:i:s"), PHP_EOL;\n    sleep(5);\n}\n',
)
SHELL = LanguageSpec(
    "shell", (".sh", ".bash", ".zsh", ".env"),
    _words("if then else elif fi case esac for select while until do done in function time return exit break "
           "continue local export readonly declare unset shift trap source set eval exec"),
    _words("true false"),
    _words("echo printf read cd pwd test sleep kill wait cat grep sed awk"),
    line_comment=("#",), block_comment=None, variables=True,
    new_file='#!/usr/bin/env bash\nset -euo pipefail\n\ntrap \'echo "stopping"; exit 0\' INT TERM\n\n'
             'echo "worker started"\nwhile true; do\n  echo "tick $(date +%H:%M:%S)"\n  sleep 5 &\n  wait $!\ndone\n',
)
BATCH = LanguageSpec(
    "batch", (".bat", ".cmd"),
    _words("if else for in do goto call exit set setlocal endlocal echo off on not exist defined errorlevel "
           "equ neq lss leq gtr geq pause rem start shift"),
    line_comment=("rem ", "REM ", "::"), block_comment=None, case_insensitive=True,
)
POWERSHELL = LanguageSpec(
    "powershell", (".ps1", ".psm1", ".psd1"),
    _words("begin break catch class continue data do dynamicparam else elseif end enum exit filter finally for "
           "foreach from function if in param process return switch throw trap try until using var while "
           "-eq -ne -gt -ge -lt -le -like -notlike -match -notmatch -and -or -not -contains -in"),
    _words("$true $false $null"),
    _words("Write-Host Write-Output Write-Error Start-Sleep Get-Date Get-Item Get-ChildItem Set-Location "
           "Invoke-RestMethod Invoke-WebRequest ForEach-Object Where-Object Select-Object"),
    line_comment=("#",), block_comment=("<#", "#>"), variables=True, case_insensitive=True,
    new_file='$ErrorActionPreference = "Stop"\n\nWrite-Output "worker started"\nwhile ($true) {\n'
             '    Write-Output "tick $(Get-Date -Format HH:mm:ss)"\n    Start-Sleep -Seconds 5\n}\n',
)
RUST = LanguageSpec(
    "rust", (".rs",),
    _words("as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod "
           "move mut pub ref return self Self static struct super trait type unsafe use where while"),
    _words("true false None Some Ok Err"),
    _words("println! print! eprintln! format! vec! panic! String Vec Option Result Box"),
)

SPECS: tuple[LanguageSpec, ...] = (GO, JAVA, CSHARP, RUBY, PHP, SHELL, BATCH, POWERSHELL, RUST)
BY_NAME = {s.name: s for s in SPECS}
BY_EXTENSION = {ext: s for s in SPECS for ext in s.extensions}

STATE_BLOCK, STATE_RAW = 5, 6
_NUM = r"\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?[fFdDlLmMuU]?)\b"


class GenericHighlighter(PythonHighlighter):
    """Highlighting for a :class:`LanguageSpec`, sharing the Python palette."""

    def __init__(self, spec: LanguageSpec, document: QTextDocument | None) -> None:
        super().__init__(document)
        self.spec = spec
        parts = []
        if spec.line_comment:
            starts = "|".join(re.escape(c) for c in spec.line_comment)
            # "rem" only counts at the start of a statement; "#" in PowerShell/shell as a word start.
            parts.append(rf"(?P<comment>(?:^|(?<=\s))(?:{starts}).*)" if spec.name in ("shell", "batch")
                         else rf"(?P<comment>(?:{starts}).*)")
        if spec.block_comment:
            parts.append(rf"(?P<block>{re.escape(spec.block_comment[0])})")
        if spec.raw_string:
            parts.append(rf"(?P<raw>{re.escape(spec.raw_string)})")
        parts.append(r"""(?P<str>'(?:\\.|[^'\\])*'?|"(?:\\.|[^"\\])*"?)""")
        if spec.variables:
            parts.append(r"(?P<var>\$\{?[A-Za-z_][\w:]*\}?)")
        parts.append(rf"(?P<num>{_NUM})")
        parts.append(r"(?P<ident>-?[A-Za-z_][\w!?-]*)" if spec.case_insensitive else r"(?P<ident>[A-Za-z_][\w!?]*)")
        self._token = re.compile("|".join(parts), re.MULTILINE)
        fold = (lambda ws: frozenset(w.lower() for w in ws)) if spec.case_insensitive else (lambda ws: ws)
        self._keywords = fold(spec.keywords)
        self._constants = fold(spec.constants)
        self._builtins = fold(spec.builtins)
        self._call = re.compile(r"\s*[(!]")

    def highlightBlock(self, text: str | None) -> None:
        if text is None:
            return
        spec = self.spec
        pos = 0
        state = self.previousBlockState()
        if state in (STATE_BLOCK, STATE_RAW):
            close = spec.block_comment[1] if state == STATE_BLOCK and spec.block_comment else spec.raw_string or ""
            end = text.find(close) if close else -1
            fmt = self.f_comment if state == STATE_BLOCK else self.f_str
            if end < 0:
                self.setFormat(0, len(text), fmt)
                self.setCurrentBlockState(state)
                return
            self.setFormat(0, end + len(close), fmt)
            pos = end + len(close)
        self.setCurrentBlockState(0)

        while (m := self._token.search(text, pos)) is not None:
            kind, start, end = m.lastgroup, m.start(), m.end()
            if kind in ("block", "raw"):
                close = (spec.block_comment[1] if kind == "block" and spec.block_comment else spec.raw_string) or ""
                found = text.find(close, end)
                fmt = self.f_comment if kind == "block" else self.f_str
                if found < 0:
                    self.setFormat(start, len(text) - start, fmt)
                    self.setCurrentBlockState(STATE_BLOCK if kind == "block" else STATE_RAW)
                    return
                self.setFormat(start, found + len(close) - start, fmt)
                pos = found + len(close)
                continue
            if kind == "comment":
                self.setFormat(start, end - start, self.f_comment)
            elif kind == "str":
                self.setFormat(start, end - start, self.f_str)
            elif kind == "var":
                fmt_var = self.f_const if m.group().lower() in self._constants else self.f_self
                self.setFormat(start, end - start, fmt_var)
            elif kind == "num":
                self.setFormat(start, end - start, self.f_num)
            elif kind == "ident":
                self._ident(text, m.group(), start, end)
            pos = max(end, start + 1)

    def _ident(self, text: str, word: str, start: int, end: int) -> None:
        key = word.lower() if self.spec.case_insensitive else word
        fmt: QTextCharFormat | None = None
        if key in self._constants:
            fmt = self.f_const
        elif key in self._keywords:
            fmt = self.f_kw
        elif key in self._builtins:
            fmt = self.f_builtin
        elif self._call.match(text, end):
            fmt = self.f_call
        elif len(word) > 1 and word.isupper():
            fmt = self.f_upper
        if fmt is not None:
            self.setFormat(start, end - start, fmt)


def spec_for(language: str) -> LanguageSpec | None:
    return BY_NAME.get(language)


def highlighter_for_spec(language: str, document: QTextDocument | None) -> QSyntaxHighlighter | None:
    spec = BY_NAME.get(language)
    return GenericHighlighter(spec, document) if spec else None
