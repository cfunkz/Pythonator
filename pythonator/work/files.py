"""The editor's view of a service folder: list, read and write files through safefs (no links, no special files).
.env files are shown masked (each value replaced) and can't be written from the editor: their values are
secrets, and the mask would overwrite them. Secrets are edited in Settings → Variables, where they go to the OS
keychain. Writes carry the hash of the text you started from, so a file that changed on disk is not overwritten."""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Any

from pythonator.safe import safefs
from pythonator.safe.mask import MASK

MAX_ENTRIES = 3000
MAX_FILE = 2 * 1024 * 1024
HIDE = frozenset({".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "venv",
                  "env", "node_modules", ".tox", ".nox", ".next", ".turbo", "target", ".gradle", ".cache", "cache",
                  "pip", "http-v2", "site-packages", "__pypackages__", ".eggs", ".idea", ".vscode", ".pythonator"})

def hidden(name: str) -> bool:
    """Folders nobody edits: version control, environments, caches and build output."""
    return name.lower() in HIDE or name.endswith((".egg-info", ".dist-info"))
_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*)(.*)$")

class FileRefused(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

def is_env(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")

def _check(rel: str) -> str:
    rel = rel.replace("\\", "/")
    if rel.startswith("/"):
        raise FileRefused("FILE-001", "That path isn't inside the service folder.")
    rel = rel.rstrip("/")
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise FileRefused("FILE-001", "That path isn't inside the service folder.")
    return rel

def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def mask_env(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        out.append(m[1] + MASK if m and m[2].strip() and not line.lstrip().startswith("#") else line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")

def tree(folder: str) -> list[dict[str, Any]]:
    """Every folder and file (depth first, folders first), without VCS folders, venvs and caches."""
    out: list[dict[str, Any]] = []

    def walk(rel: str, depth: int) -> None:
        if depth > 8 or len(out) >= MAX_ENTRIES:
            return
        try:
            entries = safefs.list_dir(folder, rel)
        except (OSError, safefs.UnsafePath):
            return
        entries = sorted(entries, key=lambda e: (e.kind != "dir", e.name.lower()))
        for e in entries:
            path = f"{rel}/{e.name}" if rel else e.name
            if e.kind == "dir":
                if hidden(e.name):
                    continue
                out.append({"path": path, "kind": "dir", "depth": depth})
                walk(path, depth + 1)
            elif e.kind == "file":
                out.append({"path": path, "kind": "file", "depth": depth, "size": e.size, "masked": is_env(path)})
            if len(out) >= MAX_ENTRIES:
                return

    walk("", 0)
    return out

def _ls(folder: str, rel: str) -> list[safefs.Entry]:
    try:
        return safefs.list_dir(folder, rel)
    except (OSError, safefs.UnsafePath):
        return []

def names(folder: str) -> list[str]:
    """Words for the editor's completion: the project's modules and what each defines (module.name, Class.method),
    and the packages in its environment with their modules. Parsed with ast, never imported or run."""
    out: set[str] = set()
    for e in tree(folder):
        if e["kind"] != "file" or not e["path"].endswith(".py") or e["size"] > 512 * 1024 or len(out) > 30_000:
            continue
        mod = e["path"][:-3].removesuffix("/__init__").removeprefix("src/").replace("/", ".")
        out.add(mod)
        try:
            body = ast.parse(safefs.read_bytes(folder, e["path"], 512 * 1024)).body
        except (OSError, safefs.UnsafePath, SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        for n in body:
            if isinstance(n, ast.ClassDef):
                out |= {f"{mod}.{n.name}.{m.name}" for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(f"{mod}.{n.name}")
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                out |= {f"{mod}.{t.id}" for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    for env in (".venv", "venv", "env"):
        libs = [f"{env}/Lib/site-packages"] + [f"{env}/lib/{e.name}/site-packages" for e in _ls(folder, f"{env}/lib")]
        for sp in libs:
            for e in _ls(folder, sp):
                pkg = e.name.removesuffix(".py")
                if pkg.isidentifier() and not pkg.startswith("_") and (e.kind == "dir" or e.name.endswith(".py")):
                    out.add(pkg)
                    out |= {f"{pkg}.{x.name.removesuffix('.py')}" for x in _ls(folder, f"{sp}/{e.name}")
                            if e.kind == "dir" and x.name.removesuffix(".py").isidentifier() and not x.name.startswith("_")}
    return sorted(out)[:40_000]

def read(folder: str, rel: str) -> dict[str, Any]:
    rel = _check(rel)
    try:
        data = safefs.read_bytes(folder, rel, MAX_FILE + 1)
    except safefs.UnsafePath as e:
        raise FileRefused("FILE-002", f"{rel}: {e}") from None
    except OSError as e:
        raise FileRefused("FILE-003", f"{rel}: {e.strerror or e}") from None
    if len(data) > MAX_FILE:
        raise FileRefused("FILE-004", f"{rel} is over 2 MB; open it in your own editor.")
    if b"\x00" in data[:8192]:
        raise FileRefused("FILE-005", f"{rel} isn't a text file.")
    text = data.decode("utf-8", "replace")
    masked = is_env(rel)
    if masked:
        text = mask_env(text)
    return {"path": rel, "text": text, "hash": digest(data.decode("utf-8", "replace")), "masked": masked, "readonly": masked}

def write(folder: str, rel: str, text: str, base: str) -> dict[str, Any]:
    rel = _check(rel)
    if is_env(rel):
        raise FileRefused("FILE-006", ".env values are masked here, so the editor can't save this file.",
                          "Change secrets in Settings → Variables.")
    if len(text.encode("utf-8")) > MAX_FILE:
        raise FileRefused("FILE-004", "Files over 2 MB can't be saved from here.")
    try:
        cur = safefs.read_bytes(folder, rel, MAX_FILE + 1).decode("utf-8", "replace")
    except FileNotFoundError:
        cur = None
    except (OSError, safefs.UnsafePath) as e:
        raise FileRefused("FILE-002", f"{rel}: {e}") from None
    if base and cur is not None and digest(cur) != base:
        raise FileRefused("FILE-007", f"{rel} changed on disk after you opened it.", "Reload it, then make the edit again.")
    try:
        safefs.write_bytes(folder, rel, text.encode("utf-8"), make_parents=cur is None)
    except (OSError, safefs.UnsafePath) as e:
        raise FileRefused("FILE-002", f"{rel}: {e}") from None
    return {"path": rel, "hash": digest(text)}

def apply_edit(folder: str, rel: str, old: str, new: str) -> dict[str, Any]:
    """An exact-text edit (the only kind the assistant can propose): old must appear exactly once in the file."""
    cur = read(folder, rel)
    if cur["masked"]:
        raise FileRefused("FILE-006", "The assistant can't edit .env files.")
    if "\r\n" in cur["text"] and "\r\n" not in old:  # a Windows (CRLF) file: proposals are written with \n
        old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    n = cur["text"].count(old) if old else 0
    if n != 1:
        raise FileRefused("EDIT-001", "The text the edit replaces " + ("isn't in the file any more." if n == 0 else
                          f"appears {n} times, so it is ambiguous."), "Ask again.")
    return write(folder, rel, cur["text"].replace(old, new, 1), cur["hash"])
