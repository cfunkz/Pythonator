"""Runtime versions: what is installed, and what a project or template needs.

The same check for every runtime, before anything runs. A project template says which versions of its runtime
its framework supports (Nuxt 4: Node.js ^22.22.3 || ^24.15.0 || >=26). A project says it in its own files
(``package.json`` engines, ``pyproject.toml`` requires-python, ``go.mod``'s go line, ``Cargo.toml``'s
rust-version, ``composer.json``'s php). When the installed one doesn't fit, Pythonator says so in plain words,
with what to install, instead of letting the start fail somewhere deep in the framework.

Ranges use npm's syntax, which covers the others: ``^22.12.0 || >=24``, ``>= 3.10``, ``~3.2``, ``22.x``, ``<4``.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

from ..paths import IS_WINDOWS, quiet_start
from . import readfile

Version = tuple[int, int, int]

# How each runtime's program says its version, and where to get a newer one.
HOW: dict[str, tuple[str, str]] = {
    "node": ("Node.js", "install the current LTS from nodejs.org"),
    "python": ("Python", "install a newer one from python.org"),
    "go": ("Go", "install a newer one from go.dev/dl"),
    "rust": ("Rust", "run: rustup update"),
    "bun": ("Bun", "run: bun upgrade"),
    "deno": ("Deno", "run: deno upgrade"),
    "java": ("Java", "install a newer JDK, for example from adoptium.net"),
    "php": ("PHP", "install a newer one from php.net"),
    "ruby": ("Ruby", "install a newer one from ruby-lang.org"),
}
_seen: dict[tuple[str, int], Version | None] = {}


def parse(text: str) -> Version | None:
    """The first version number in ``text`` (``v22.13.0``, ``Python 3.13.1``, ``go1.24.7``, ``openjdk 21``)."""
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    return (int(m[1]), int(m[2] or 0), int(m[3] or 0)) if m else None


def installed(prog: str) -> Version | None:
    """The version of the program at ``prog`` (asked once per program file), or None."""
    try:
        key = (prog, Path(prog).stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _seen:
        stem = Path(prog).stem.lower()
        flag = "-version" if stem == "java" else "version" if stem == "go" else "--version"
        try:
            done = subprocess.run([prog, flag], capture_output=True, text=True, timeout=20, check=False,
                                  creationflags=0x08000000 if IS_WINDOWS else 0, startupinfo=quiet_start())
            _seen[key] = parse((done.stdout or "") + "\n" + (done.stderr or "")) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            _seen[key] = None
    return _seen[key]


def ranges(spec: str) -> list[tuple[Version, Version | None]]:
    """npm-style ranges as [low, high) pairs, one per ``||`` alternative."""
    out: list[tuple[Version, Version | None]] = []
    for part in re.split(r"\|\|?", spec):  # composer writes ^8.2|^8.3
        low: Version = (0, 0, 0)
        high: Version | None = None
        found = False
        for op, text in re.findall(r"(\^|~=|~|>=|>|<=|<|==|!=|=)?\s*v?(\d+(?:\.(?:\d+|x|\*)){0,2})", part):
            if op == "!=":  # one excluded version: too fine to matter here
                continue
            numbers = [n for n in text.split(".") if n not in ("x", "*")]
            v = parse(".".join(numbers))
            if v is None:
                continue
            found = True
            if op == "^":
                low, high = v, ((v[0] + 1, 0, 0) if v[0] else (0, v[1] + 1, 0))
            elif op in ("~", "~="):  # ~=3.10 (Python): 3.x from 3.10
                low, high = v, ((v[0] + 1, 0, 0) if op == "~=" and len(numbers) == 2 else (v[0], v[1] + 1, 0))
            elif op in (">=", ">"):
                low = v if op == ">=" else (v[0], v[1], v[2] + 1)
            elif op in ("<", "<="):
                high = v if op == "<" else (v[0], v[1], v[2] + 1)
            elif len(numbers) < 3:  # 22, 22.x, 1.22: that major (or minor)
                low, high = v, ((v[0] + 1, 0, 0) if len(numbers) == 1 else (v[0], v[1] + 1, 0))
            else:
                low, high = v, (v[0], v[1], v[2] + 1)
        if found:
            out.append((low, high))
    return out


def satisfies(version: Version, spec: str) -> bool:
    """Whether ``version`` is in ``spec``; True when the spec says nothing this can read."""
    parsed = ranges(spec)
    return not parsed or any(low <= version and (high is None or version < high) for low, high in parsed)


def text(v: Version) -> str:
    return ".".join(map(str, v))


def shortfall(runtime: str, version: Version | None, spec: str) -> tuple[str, str]:
    """When ``version`` doesn't fit ``spec``: what is needed ("Node.js 22.22.3 or newer (this computer has
    22.13.0)") and how to get it; ("", "") when it fits or isn't known."""
    if version is None or not spec or satisfies(version, spec):
        return "", ""
    name, how = HOW.get(runtime, (runtime, "install a newer one"))
    newer = sorted(low for low, _high in ranges(spec) if low > version)
    want = f"{name} {text(newer[0])} or newer" if newer else f"{name} {spec}"
    return f"{want} (this computer has {text(version)})", how


def problem(runtime: str, version: Version | None, spec: str, what: str = "It") -> str:
    """"" when ``version`` fits ``spec`` (or isn't known), else a sentence saying what to do."""
    want, how = shortfall(runtime, version, spec)
    return f"{what} needs {want}: {how}" if want else ""


def declared(runtime: str, folder: Path) -> tuple[str, str]:
    """The versions a project says it needs, and where it says so (("", "") when it doesn't)."""
    try:
        if runtime in ("node", "bun") and (f := folder / "package.json").is_file():
            engines = json.loads(readfile.read_text(f)).get("engines") or {}
            spec = engines.get("bun" if runtime == "bun" else "node") if isinstance(engines, dict) else None
            return (str(spec), "package.json (engines)") if isinstance(spec, str) and spec else ("", "")
        if runtime == "python" and (f := folder / "pyproject.toml").is_file():
            spec = tomllib.loads(readfile.read_text(f)).get("project", {}).get("requires-python")
            return (str(spec), "pyproject.toml (requires-python)") if isinstance(spec, str) else ("", "")
        if runtime == "go" and (f := folder / "go.mod").is_file():
            m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", readfile.read_text(f), re.M)
            if not m or (wanted := parse(m[1])) is None:
                return "", ""
            # From Go 1.21, go fetches the newer toolchain a go.mod asks for by itself: only older ones can't.
            return (">=1.21" if wanted >= (1, 21, 0) else f">={m[1]}"), "go.mod (go line)"
        if runtime == "rust" and (f := folder / "Cargo.toml").is_file():
            spec = tomllib.loads(readfile.read_text(f)).get("package", {}).get("rust-version")
            return (f">={spec}", "Cargo.toml (rust-version)") if isinstance(spec, str) else ("", "")
        if runtime == "php" and (f := folder / "composer.json").is_file():
            require = json.loads(readfile.read_text(f)).get("require") or {}
            spec = require.get("php") if isinstance(require, dict) else None
            return (str(spec), "composer.json (require php)") if isinstance(spec, str) else ("", "")
    except (OSError, ValueError, AttributeError, RecursionError):  # TOMLDecodeError is a ValueError
        pass
    return "", ""
