"""Fit check: read a folder (never run it) and suggest how it should run.
Reads through the safe reader only (no links, no special files, capped sizes, at most 2,000 files, 3 levels deep).
Finds the runtime and its manifest, how many packages and how many build native code, whether it talks to the
network (and the hosts written in its code), whether it listens on a port, and whether it is a database or already
a container. The answer is advice: you can pick any mode."""

from __future__ import annotations

import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pythonator.safe import safefs

MAX_FILES = 2000
MAX_DEPTH = 3
SKIP_DIRS = {".git", ".hg", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".tox", "bin", "obj"}
SOURCE = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node", ".go": "go", ".rs": "rust",
          ".rb": "ruby", ".java": "java", ".kt": "java", ".cs": "dotnet", ".ps1": "powershell"}
# Packages that build or ship native code: they need system libraries and are the usual reason to pick Container.
NATIVE = {"numpy", "scipy", "pandas", "lxml", "psycopg2", "psycopg2-binary", "mysqlclient", "pillow", "cryptography",
          "grpcio", "uvloop", "orjson", "pydantic-core", "torch", "opencv-python", "bcrypt", "sharp", "sqlite3", "better-sqlite3",
          "canvas", "bufferutil", "node-gyp", "argon2", "pyzmq", "zeromq"}
NET_HINTS = re.compile(r"\b(requests|httpx|aiohttp|urllib|socket|websockets?|grpc|boto3|redis|psycopg|pymongo|fetch\(|axios|"
                       r"net/http|reqwest|HttpClient|Net::HTTP|Invoke-WebRequest)\b")
LISTEN_HINTS = re.compile(r"(\.listen\(|uvicorn|gunicorn|app\.run\(|http\.server|ListenAndServe|serve\(|bind\(\(|"
                          r"\bPORT\b|--port\b|Kestrel|WEBrick|express\(\))")
PORT_NUM = re.compile(r"(?:port\s*[=:]\s*|--port[ =]|listen\(\s*|:)(\d{4,5})\b", re.IGNORECASE)
GUI_HINTS = re.compile(r"^\s*(?:from|import)\s+(PyQt[56]|PySide[26]|tkinter|customtkinter|wx|kivy|pygame|dearpygui|flet|"
                       r"toga)\b|require\(['\"]electron['\"]\)|from ['\"]electron['\"]", re.MULTILINE)
GUI_PACKAGES = {"pyqt5", "pyqt6", "pyside2", "pyside6", "customtkinter", "wxpython", "kivy", "pygame", "dearpygui", "flet",
                "toga", "electron"}
HOST_IN_CODE = re.compile(r"""["'](?:https?|wss?)://([a-z0-9.-]+\.[a-z]{2,})(?::\d+)?[/"']""", re.IGNORECASE)
DATABASES = {"postgres": "postgres:17", "postgresql": "postgres:17", "mysql": "mysql:8.4", "mariadb": "mariadb:11",
             "redis": "redis:7", "valkey": "valkey/valkey:8", "mongo": "mongo:8", "mongodb": "mongo:8",
             "elasticsearch": "elasticsearch:8.15.0", "rabbitmq": "rabbitmq:4", "clickhouse": "clickhouse/clickhouse-server:24"}
DB_PORTS = {"postgres": 5432, "mysql": 3306, "mariadb": 3306, "redis": 6379, "valkey": 6379, "mongo": 27017,
            "elasticsearch": 9200, "rabbitmq": 5672, "clickhouse": 8123}
COMMANDS = (("main.py", "python"), ("app.py", "python"), ("server.py", "python"), ("run.py", "python"), ("worker.py", "python"),
            ("index.js", "node"), ("server.js", "node"), ("main.js", "node"), ("index.ts", "node"), ("main.go", "go"),
            ("main.rb", "ruby"), ("app.rb", "ruby"), ("main.ps1", "powershell"))

@dataclass
class Fit:
    runtime: str = ""  # python | node | bun | deno | go | rust | java | dotnet | ruby | powershell | binary
    runtime_label: str = ""  # "Python 3.13"
    manifest: str = ""  # pyproject, requirements.txt, package.json, go.mod, Cargo.toml…
    packages: int = 0
    native: list[str] = field(default_factory=list)
    network: bool = False
    network_file: str = ""
    hosts: list[str] = field(default_factory=list)
    listens: bool = False
    port: int = 0
    database: str = ""  # postgres, redis…
    gui: bool = False  # it opens windows (PyQt, Tk, Electron…)
    image: str = ""  # suggested image for Container
    command: list[str] = field(default_factory=list)
    name: str = ""
    mode: str = "sandbox"
    why: str = ""
    warnings: list[str] = field(default_factory=list)
    files: int = 0
    took_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

def scan(folder: str) -> Fit:
    t0 = time.perf_counter()
    fit = Fit(name=_name(folder))
    files: list[tuple[str, int]] = []
    _walk(folder, "", 0, files)
    fit.files = len(files)
    names = {rel.lower(): size for rel, size in files}
    _manifests(folder, names, fit)
    counts: dict[str, int] = {}
    for rel, size in files:
        ext = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext in SOURCE:
            counts[SOURCE[ext]] = counts.get(SOURCE[ext], 0) + 1
        if ext in SOURCE and size < 512 * 1024 and (not fit.network or not fit.listens or not fit.gui or len(fit.hosts) < 6):
            _source(folder, rel, fit)
    if not fit.runtime and counts:
        fit.runtime = max(counts, key=lambda k: counts[k])
        fit.runtime_label = fit.runtime_label or fit.runtime.capitalize()
    if not fit.command:
        fit.command = _command(names, fit.runtime)
    _decide(fit, names)
    fit.hosts = sorted(set(fit.hosts))[:8]
    fit.took_ms = round((time.perf_counter() - t0) * 1000, 1)
    return fit

def _name(folder: str) -> str:
    base = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:48] or "service"

def _walk(root: str, rel: str, depth: int, out: list[tuple[str, int]]) -> None:
    if depth > MAX_DEPTH or len(out) >= MAX_FILES:
        return
    try:
        entries = safefs.list_dir(root, rel)
    except (OSError, safefs.UnsafePath):
        return
    for e in entries:
        path = f"{rel}/{e.name}" if rel else e.name
        if e.kind == "file":
            out.append((path, e.size))
            if len(out) >= MAX_FILES:
                return
        elif e.kind == "dir" and e.name not in SKIP_DIRS and not e.name.startswith("."):
            _walk(root, path, depth + 1, out)

def _read(root: str, rel: str, limit: int = 256 * 1024) -> str:
    try:
        return safefs.read_text(root, rel, limit=limit)
    except (OSError, safefs.UnsafePath, UnicodeDecodeError, ValueError):
        return ""

def _manifests(root: str, names: dict[str, int], fit: Fit) -> None:
    import json
    import tomllib

    if "pyproject.toml" in names or "requirements.txt" in names or "pipfile" in names:
        fit.runtime = "python"
        deps: list[str] = []
        if "pyproject.toml" in names:
            fit.manifest = "pyproject"
            try:
                data = tomllib.loads(_read(root, "pyproject.toml"))
                deps = list(data.get("project", {}).get("dependencies", []))
                need = str(data.get("project", {}).get("requires-python", ""))
                m = re.search(r"3\.(\d+)", need)
                fit.runtime_label = f"Python 3.{max(int(m.group(1)), 12)}" if m else "Python 3.13"
                for script in (data.get("project", {}).get("scripts") or {}):
                    fit.command = fit.command or ["-m", str(script).replace("-", "_")]
            except (tomllib.TOMLDecodeError, ValueError):
                fit.warnings.append("pyproject.toml couldn't be read")
        if "requirements.txt" in names:
            fit.manifest = fit.manifest or "requirements.txt"
            deps += [x for x in _read(root, "requirements.txt").splitlines() if x.strip() and not x.lstrip().startswith(("#", "-"))]
        fit.runtime_label = fit.runtime_label or "Python 3.13"
        fit.packages = len(deps)
        fit.native = sorted({_pkg(d) for d in deps if _pkg(d) in NATIVE})
        fit.gui = any(_pkg(d) in GUI_PACKAGES for d in deps)
    elif "package.json" in names:
        fit.manifest = "package.json"
        try:
            data = json.loads(_read(root, "package.json"))
        except ValueError:
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        fit.packages = len(deps)
        fit.native = sorted(d for d in deps if d in NATIVE)
        fit.gui = "electron" in deps
        if "bun.lockb" in names or "bun.lock" in names:
            fit.runtime, fit.runtime_label = "bun", "Bun"
        else:
            fit.runtime, fit.runtime_label = "node", "Node 22"
        start = (data.get("scripts") or {}).get("start", "")
        main = data.get("main", "")
        if start.startswith("node "):
            fit.command = start.split()[1:]
        elif main:
            fit.command = [main]
    elif "deno.json" in names or "deno.jsonc" in names:
        fit.runtime, fit.runtime_label, fit.manifest = "deno", "Deno 2", "deno.json"
    elif "go.mod" in names:
        fit.runtime, fit.runtime_label, fit.manifest = "go", "Go", "go.mod"
        fit.packages = sum(1 for x in _read(root, "go.mod").splitlines() if x.startswith("\t"))
        fit.command = ["run", "."]
    elif "cargo.toml" in names:
        fit.runtime, fit.runtime_label, fit.manifest = "rust", "Rust", "Cargo.toml"
        fit.command = ["run", "--release"]
    elif "gemfile" in names:
        fit.runtime, fit.runtime_label, fit.manifest = "ruby", "Ruby", "Gemfile"
    elif "pom.xml" in names or "build.gradle" in names or "build.gradle.kts" in names:
        fit.runtime, fit.runtime_label, fit.manifest = "java", "Java 21", "pom.xml" if "pom.xml" in names else "Gradle"
    elif any(n.endswith((".csproj", ".fsproj")) for n in names):
        fit.runtime, fit.runtime_label, fit.manifest = "dotnet", ".NET 8", next(n for n in names if n.endswith((".csproj", ".fsproj")))
        fit.command = ["run"]
    for compose in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        if compose in names:
            text = _read(root, compose).lower()
            for db in DATABASES:
                if re.search(rf"image:\s*['\"]?{db}", text):
                    fit.database = db.replace("postgresql", "postgres").replace("mongodb", "mongo")
                    break
            m = re.search(r"image:\s*['\"]?([a-z0-9./_-]+(?::[a-z0-9._-]+)?)", text)
            if m:
                fit.image = m.group(1)
    if "dockerfile" in names or "containerfile" in names:
        fit.manifest = fit.manifest or "Dockerfile"
        fit.image = fit.image or "(builds from Dockerfile)"
    for marker, db in (("pg_version", "postgres"), ("postgresql.conf", "postgres"), ("redis.conf", "redis"), ("my.cnf", "mysql"),
                       ("dump.rdb", "redis"), ("mongod.conf", "mongo")):
        if marker in names:
            fit.database = fit.database or db

def _pkg(dep: str) -> str:
    return re.split(r"[\s<>=!~\[;@]", dep.strip(), maxsplit=1)[0].lower()

def _source(root: str, rel: str, fit: Fit) -> None:
    text = _read(root, rel)
    if not text:
        return
    if not fit.gui and GUI_HINTS.search(text):
        fit.gui = True
    if not fit.network and NET_HINTS.search(text):
        fit.network, fit.network_file = True, rel.rsplit("/", 1)[-1]
    if not fit.listens and LISTEN_HINTS.search(text):
        fit.listens = True
        m = PORT_NUM.search(text)
        if m and 1024 <= int(m.group(1)) <= 65535:
            fit.port = int(m.group(1))
    for h in HOST_IN_CODE.findall(text):
        h = h.lower()
        if h not in ("localhost", "example.com") and not h.endswith((".local", ".internal", ".test")) and len(fit.hosts) < 12:
            fit.hosts.append(h)

def _command(names: dict[str, int], runtime: str) -> list[str]:
    if "manage.py" in names:
        return ["manage.py", "runserver", "--noreload"]
    for f, rt in COMMANDS:
        if f in names and (not runtime or runtime == rt or (runtime == "bun" and rt == "node")):
            return [f]
    return []

def _decide(fit: Fit, names: dict[str, int]) -> None:
    if fit.database:
        fit.mode = "container"
        fit.image = DATABASES.get(fit.database, fit.image)
        fit.port = fit.port or DB_PORTS.get(fit.database, 0)
        fit.listens = True
        fit.runtime, fit.runtime_label = "binary", fit.database.capitalize()
        fit.why = f"a {fit.database.capitalize()} database runs best from its official image, pinned by digest"
        fit.command = fit.command or [fit.database]
        return
    if fit.image and not fit.runtime:
        fit.mode = "container"
        fit.why = "it already describes its own image"
        return
    if fit.gui:
        if sys.platform == "win32":
            fit.why = "it opens windows, and a Windows sandbox may show its own windows"
        else:  # a jail with your display could read other windows and keys
            fit.mode, fit.why = "full", "it opens windows, which only Full access can show on this OS"
        return
    if fit.native:
        fit.mode = "sandbox"
        fit.why = f"{', '.join(fit.native[:3])} ship native code; it still fits Sandbox, Container is the fallback"
        return
    if not fit.runtime:
        fit.why = "no runtime recognised; tell it what to run"
        return
    fit.why = "no privileged calls, no Docker socket, writes only inside its folder"
