"""A plugin's view of a service's folder (``read_files``) and the changes it may suggest (``propose_changes``).

The plugin never gets the folder itself: its sandbox has no access to it. It asks the host, and the host decides,
read-only, file by file:

* Paths must stay inside the service's folder, links included. Version-control, virtual-environment and dependency
  folders are skipped.
* Secrets are never handed out: ``.env`` files (and the service's own env file), private keys, certificates,
  credential and token files. The values of the service's environment variables are blanked in whatever is read.
* Text only, 256 KB per file at most.

A suggested change is a list of exact search-and-replace edits. The host applies them to the file as it is now and
keeps the result as a *proposal*; nothing is written. The window shows the diff, and only the user's Apply writes it
(after checking that the file hasn't changed since, and keeping a backup). Some changes are refused outright, so the
user can't approve them by accident: changes to scripts and programs, emptying a file, and code that runs shell
commands, deletes files or evaluates code.
"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_READ = 256 * 1024
MAX_LIST = 3000
MAX_VISIT = 30_000
MAX_EDITS = 20
MAX_NEW_FILE = 48 * 1024
MAX_RESULT = 512 * 1024
MAX_DIFF = 300_000
PROPOSAL_TTL = 30 * 60

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env", ".env", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "site-packages", "dist", "build",
    ".pythonator", ".cache", "target", ".gradle", ".next", ".terraform", ".ssh", ".gnupg", ".aws", ".azure",
    ".kube", ".docker",
}
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.crt", "*.cer",
    "*.der", "*.p8", "*.asc", "*.gpg", "*.kdbx", "*.ovpn", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.ppk", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd", "credentials",
    "service-account*.json", "auth.json", "*.sqlite", "*.sqlite3", "*.db", "wallet.dat", "*.tfstate", "*.tfvars",
    "kubeconfig", ".dockercfg", "*.session",
)
# A data file named like a secret (tokens.json, secrets.yaml, credentials.ini) is one; source code isn't.
_SECRET_WORD = re.compile(r"secret|token|credential|password|passwd|apikey|api_key|api-key|private", re.IGNORECASE)
CODE_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb",
                 ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".swift", ".lua", ".dart", ".md", ".rst", ".html", ".css"}
# Programs and scripts the assistant may not change: they run commands by design.
BLOCKED_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".psd1", ".vbs",
    ".vbe", ".wsf", ".wsh", ".lnk", ".reg", ".scr", ".msi", ".msp", ".sys", ".sh", ".bash", ".zsh",
    ".fish", ".command", ".service", ".plist", ".desktop", ".scpt", ".applescript", ".jar", ".app",
}
BLOCKED_NAMES = {"Makefile", "makefile", "Dockerfile", "Containerfile", "Procfile", ".gitattributes", ".gitmodules",
                 "crontab", ".bashrc", ".profile", ".zshrc"}
DEPENDENCY_FILES = ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "package.json",
                    "go.mod", "Cargo.toml", "Gemfile", "composer.json", "*.csproj", "pom.xml", "build.gradle*")

# Added lines matching these are refused: shell commands, process spawning, deleting files, evaluating code.
_BLOCK = [(re.compile(p, re.IGNORECASE), why) for p, why in [
    (r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*|startfile|fork)\b|\bpty\.spawn\b"
     r"|\basyncio\.create_subprocess|\bcommands\.getoutput", "runs programs or shell commands"),
    (r"\bchild_process\b|\bexecSync\b|\bspawnSync\b|\bexecFile\b|\bDeno\.run\b|\bBun\.spawn\b|\bRuntime\.getRuntime\(\)"
     r"\.exec|\bProcessBuilder\b|\bexec\.Command\b|\bstd::process::Command\b", "runs programs or shell commands"),
    (r"\b(powershell|pwsh|cmd(\.exe)?\s*/c|bash\s+-c|sh\s+-c|zsh\s+-c|Invoke-Expression|iex\s*\(|Start-Process)\b",
     "runs shell commands"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(ba|z)?sh\b", "downloads and runs a script"),
    (r"\brm\s+-[a-z]*[rf]|\bdel\s+/[sfq]|\brmdir\s+/s|\bRemove-Item\b|\bmkfs\b|\bdd\s+if=|\bformat\s+[a-z]:|"
     r"\bvssadmin\b|\bbcdedit\b|\breg\s+delete\b|\bcipher\s+/w|\bshred\b|\bsudo\b|\bchmod\s+-R\b|\bchown\s+-R\b",
     "runs destructive system commands"),
    (r"\bshutil\.(rmtree|move)\b|\bos\.(remove|unlink|rmdir|removedirs|truncate)\b|\.unlink\(|\.rmdir\(|"
     r"\bfs\.(rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync|truncate)\b|\brimraf\b|\bos\.RemoveAll\b|"
     r"\bremove_dir_all\b|\bFile\.Delete\b|\bDirectory\.Delete\b", "deletes files or folders"),
    (r"(?<![\w.])(eval|exec)\s*\(|\b__import__\s*\(|\bcompile\s*\([^)]*['\"]exec['\"]|\bnew\s+Function\s*\(|"
     r"\bvm\.runIn\w*", "evaluates code from text"),
    (r"\bctypes\b|\bcffi\b|\bwinreg\b|\b_winapi\b|\bmsvcrt\b", "calls the operating system directly"),
    (r"\bfrom\s+(os|shutil|pty)\s+import\b", "imports process or file-deleting calls under another name"),
    (r"\bgetattr\s*\([^)]*['\"]\s*\+|\b__builtins__\b|\bbuiltins\.", "looks up a function by a built-up name"),
    (r"['\"](pre|post)?(install|prepare|prepublish)['\"]\s*:", "adds a script npm runs on Install"),
]]
# Never in an added line: an edit block's own markers (a model that gets the format wrong would otherwise write
# them into the file and break it), or the stand-in for a hidden value (it would overwrite the real secret).
_LEFTOVER = re.compile(r"^[ \t]*(?:<{7}[ \t]+(?:EDIT|SEARCH)\b.*|={7}[ \t]*|>{7}[ \t]+(?:END|REPLACE)\b.*)$"
                       r"|\[secret removed\]|\[[A-Za-z_][A-Za-z0-9_]* hidden\]", re.MULTILINE)
# Added lines matching these are allowed, with a warning in the review window.
_WARN = [(re.compile(p, re.IGNORECASE), why) for p, why in [
    (r"https?://", "adds a web address"),
    (r"\b(requests|httpx|aiohttp|urllib|http\.client|socket|fetch|axios|websocket)\b", "uses the network"),
    (r"\b(pickle|marshal|shelve|yaml\.load)\b", "loads data that can run code"),
    (r"\bbase64\b|\bb64decode\b|\batob\b", "decodes base64"),
    (r"\bopen\([^)]*['\"][wax]\+?b?['\"]|\.write_(text|bytes)\(|\bwriteFile", "writes files"),
    (r"\b(os\.environ|process\.env|getenv)\b", "reads environment variables"),
    (r"\bimportlib\b|\brequire\s*\(\s*[^'\"]", "imports code by name"),
]]


class FileRefused(ValueError):
    """Shown to the plugin (and in the review window) as is."""


@dataclass
class Proposal:
    id: str
    plugin: str
    service: str
    path: str  # relative, with forward slashes
    summary: str
    before: str | None  # None: a new file
    after: str
    warnings: list[str]
    created: float = field(default_factory=time.monotonic)

    @property
    def before_hash(self) -> str:
        return _hash(self.before)


def _hash(text: str | None) -> str:
    return "" if text is None else hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def is_secret(rel: str, env_file: str = "") -> bool:
    parts = PurePosixPath(rel).parts
    name = parts[-1] if parts else ""
    if env_file:
        with contextlib.suppress(FileRefused):
            if clean_rel(env_file) == rel:
                return True
    lowered = name.lower()
    if any(fnmatch.fnmatchcase(lowered, p) for p in SECRET_PATTERNS):
        return True
    if _SECRET_WORD.search(lowered) and PurePosixPath(lowered).suffix not in CODE_SUFFIXES:
        return True
    return any(part.lower() in (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker") for part in parts[:-1])


def clean_rel(raw: str) -> str:
    """A safe relative path with forward slashes, or FileRefused."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 400 or "\0" in raw or not raw.isprintable():
        raise FileRefused("Not a file path")
    text = raw.strip().replace("\\", "/")
    if text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", text) or "://" in text:
        raise FileRefused("Only paths inside the service's folder")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise FileRefused("Only paths inside the service's folder")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        raise FileRefused(f"{'/'.join(parts)} is in a folder plugins can't see")
    return "/".join(parts)


def resolve(folder: Path, rel: str) -> Path:
    """``rel`` in the folder. No part of it may be a link: the service can make links in its folder, and the
    rules here (secrets, scripts) are about the name asked for, not where a link leads (readme.md -> .env)."""
    root = folder.resolve()
    target = root
    for part in PurePosixPath(rel).parts:
        target = target / part
        if target.is_symlink():
            raise FileRefused(f"{rel} goes through a link, so plugins can't use it")
    if not target.resolve().is_relative_to(root):
        raise FileRefused("That path leads outside the service's folder")
    return target


class ServiceFiles:
    """The rules above, for one service: its folder, its env file, and its environment values to blank out."""

    def __init__(self, folder: Path, env_file: str = "", env_values: dict[str, str] | None = None) -> None:
        self.folder = folder
        self.env_file = env_file
        # Longest first, so a value that contains another is blanked whole.
        self.values = sorted(((k, v) for k, v in (env_values or {}).items() if len(v) >= 6),
                             key=lambda kv: -len(kv[1]))

    def _touches_hidden(self, text: str, start: int, length: int) -> bool:
        """Whether text[start:start + length] overlaps a hidden value in ``text``."""
        for _name, value in self.values:
            at = text.find(value)
            while at != -1:
                if at < start + length and start < at + len(value):
                    return True
                at = text.find(value, at + 1)
        return False

    def blank(self, text: str) -> str:
        for name, value in self.values:
            if value in text:
                text = text.replace(value, f"[{name} hidden]")
        return text

    def listing(self) -> dict[str, Any]:
        root = self.folder.resolve()
        files: list[dict[str, Any]] = []
        hidden = visited = 0
        truncated = False
        if not root.is_dir():
            raise FileRefused("The service's folder doesn't exist")
        for here, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(here, d).is_symlink())
            for name in sorted(names):
                visited += 1
                if visited > MAX_VISIT or len(files) >= MAX_LIST:
                    truncated = True
                    break
                path = Path(here, name)
                rel = path.relative_to(root).as_posix()
                if path.is_symlink() or is_secret(rel, self.env_file):
                    hidden += 1
                    continue
                with contextlib.suppress(OSError):
                    files.append({"path": rel, "size": path.stat().st_size})
            if truncated:
                break
        return {"files": files, "hidden": hidden, "truncated": truncated}

    def read(self, raw: str) -> dict[str, Any]:
        rel = clean_rel(raw)
        if is_secret(rel, self.env_file):
            raise FileRefused(f"{rel} may hold secrets, so plugins can't read it")
        path = resolve(self.folder, rel)
        if not path.is_file():
            raise FileRefused(f"{rel} isn't a file")
        with path.open("rb") as fh:
            data = fh.read(MAX_READ + 1)
        if b"\0" in data[:8192]:
            raise FileRefused(f"{rel} isn't a text file")
        truncated = len(data) > MAX_READ
        text = data[:MAX_READ].decode("utf-8", "replace")
        return {"path": rel, "text": self.blank(text), "truncated": truncated, "size": path.stat().st_size}

    def current(self, rel: str) -> str | None:
        """The whole file, unblanked (for applying edits), or None if it doesn't exist."""
        path = resolve(self.folder, rel)
        if not path.exists():
            return None
        if not path.is_file():
            raise FileRefused(f"{rel} isn't a file")
        if path.stat().st_size > MAX_RESULT:
            raise FileRefused(f"{rel} is larger than 512 KB; change it yourself")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise FileRefused(f"{rel} isn't a text file")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise FileRefused(f"{rel} isn't UTF-8 text") from None

    # -- proposals --

    def propose(self, pid: str, sid: str, raw_path: str, edits: Any, summary: str) -> Proposal:
        rel = clean_rel(raw_path)
        name = PurePosixPath(rel).name
        if is_secret(rel, self.env_file):
            raise FileRefused(f"{rel} may hold secrets; the assistant can't change it")
        suffix = "".join(PurePosixPath(rel).suffixes[-1:]).lower()
        if suffix in BLOCKED_SUFFIXES or name in BLOCKED_NAMES or rel.startswith((".github/", ".gitlab-ci")):
            raise FileRefused(f"{rel} is a script or program; the assistant can't change those")
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
            raise FileRefused(f"A change has 1 to {MAX_EDITS} edits")
        before = self.current(rel)
        text = before
        for i, edit in enumerate(edits, 1):
            if not isinstance(edit, dict) or not isinstance(edit.get("old"), str) or not isinstance(edit.get("new"),
                                                                                                  str):
                raise FileRefused("An edit is {old, new}")
            old, new = edit["old"], edit["new"]
            if text is None:
                if old or len(edits) != 1:
                    raise FileRefused(f"{rel} doesn't exist; a new file is one edit with nothing to replace")
                if len(new.encode()) > MAX_NEW_FILE:
                    raise FileRefused("A new file from the assistant is at most 48 KB")
                text = new
                continue
            if not old:
                raise FileRefused(f"Edit {i}: {rel} exists, so say which text to replace")
            count = text.count(old)
            if count == 0 and "\r\n" in text and "\r\n" not in old:  # the model saw the file with \n line ends
                old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
                count = text.count(old)
            if count != 1 or self._touches_hidden(text, text.index(old), len(old)):
                # One answer for all three: else a plugin could guess a hidden value a character at a time
                raise FileRefused(f"Edit {i}: the text to replace isn't in {rel} exactly once, or it touches a "
                                  "hidden value (the file may have changed)")
            text = text.replace(old, new, 1)
        if text is None or text == before:
            raise FileRefused("The change doesn't change anything")
        if len(text.encode()) > MAX_RESULT:
            raise FileRefused("The result would be larger than 512 KB")
        if not text.strip():
            raise FileRefused("The assistant can't empty or delete a file")
        old_lines = (before or "").splitlines()
        new_lines = text.splitlines()
        if len(old_lines) >= 20 and len(new_lines) < len(old_lines) * 0.2:
            raise FileRefused("The change removes most of the file; the assistant can't do that")
        warnings = check_added(old_lines, new_lines)
        if any(fnmatch.fnmatchcase(name, p) for p in DEPENDENCY_FILES):
            warnings.append("It changes what gets installed: packages are installed when you press Install")
        return Proposal(_new_id(), pid, sid, rel, summary.strip()[:300], before, text, warnings)

    def apply(self, proposal: Proposal, backups: Path) -> str:
        """Write it, if the file is still as it was. Returns where the backup is ("" for a new file)."""
        now = self.current(proposal.path)
        if _hash(now) != proposal.before_hash:
            raise FileRefused(f"{proposal.path} changed after the change was suggested; ask again")
        target = resolve(self.folder, proposal.path)
        backup = ""
        if proposal.before is not None:
            backups.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = backups / f"{stamp}-{proposal.path.replace('/', '__')}"
            shutil.copy2(target, dest)
            backup = str(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(proposal.after)
            if proposal.before is not None:
                shutil.copymode(target, tmp)
            Path(tmp).replace(target)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise
        return backup


def check_added(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Refuse (FileRefused) or warn about what the added lines do."""
    added = [line[1:] for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0)
             if line.startswith("+") and not line.startswith("+++")]
    body = "\n".join(added)
    if match := _LEFTOVER.search(body):
        raise FileRefused(f"The change would write {match.group(0).strip()[:40]!r} into the file: an edit marker or a "
                          "stand-in for a hidden value, not code. The suggestion is malformed; ask again")
    for pattern, why in _BLOCK:
        if match := pattern.search(body):
            raise FileRefused(f"Blocked: the change {why} ({match.group(0).strip()[:40]}). Pythonator doesn't let "
                              "the assistant add that; make the change yourself if you really want it")
    warnings = []
    for pattern, why in _WARN:
        if match := pattern.search(body):
            warnings.append(f"It {why} ({match.group(0).strip()[:40]})")
    return warnings


def diff(proposal: Proposal) -> str:
    lines = difflib.unified_diff((proposal.before or "").splitlines(), proposal.after.splitlines(),
                                 fromfile=f"a/{proposal.path}" if proposal.before is not None else "/dev/null",
                                 tofile=f"b/{proposal.path}", lineterm="")
    text = "\n".join(lines)
    return text if len(text) <= MAX_DIFF else text[:MAX_DIFF] + "\n… (the rest of the diff is cut)"


def _new_id() -> str:
    return hashlib.sha256(os.urandom(16)).hexdigest()[:16]
