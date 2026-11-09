"""The runtimes a service can use: one table for the compiler, the Fit check, New service and the Engines page.
`prefix` is put before the service's command: for Python the command is the script and its arguments
(`main.py --port 8000`); for tool runtimes the command is the tool's arguments (Go: `run .`, .NET: `run` or
`app.dll`, Rust: `run --release`). Python versions come from uv (`runtime_version`), everything else is the runtime
found on this machine. The runtime's install folder is the only part of it the jail can see, read-only."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Runtime:
    key: str
    label: str
    exe: tuple[str, ...]  # names to look for, first found wins
    prefix: tuple[str, ...] = ()  # arguments after the executable, before the command
    env: tuple[tuple[str, str], ...] = ()  # extra environment ({home} is the service's private home)
    install: str = ""  # how to get it, for the refusal
    image: str = ""  # Container mode default image
    example: str = ""  # a typical command

RUNTIMES: dict[str, Runtime] = {r.key: r for r in (
    Runtime("python", "Python (uv)", ("python3", "python"), ("-u",), install="Pythonator installs it with uv: "
            "Engines → Runtimes → install Python 3.13", image="python:3.13-slim", example="main.py --port 8000"),
    Runtime("node", "Node.js", ("node",), env=(("NODE_ENV", "production"),), install="Install Node.js 20 or newer.",
            image="node:22-slim", example="server.js"),
    Runtime("bun", "Bun", ("bun",), env=(("BUN_INSTALL_CACHE_DIR", "{home}/.bun-cache"),),
            install="Install Bun: https://bun.sh", image="oven/bun:1", example="run index.ts"),
    Runtime("deno", "Deno", ("deno",), ("run", "--allow-all", "--no-prompt"),
            (("DENO_DIR", "{home}/.deno"), ("DENO_NO_UPDATE_CHECK", "1")),
            install="Install Deno 2: https://deno.com", image="denoland/deno:2.1.4", example="main.ts"),
    Runtime("go", "Go", ("go",), env=(("GOPATH", "{home}/go"), ("GOCACHE", "{home}/.cache/go"), ("GOTOOLCHAIN", "local"),
                                       ("GOFLAGS", "-mod=readonly"), ("GOTELEMETRY", "off")),
            install="Install Go 1.22 or newer: https://go.dev/dl", image="golang:1.23", example="run ."),
    Runtime("java", "Java", ("java",), install="Install a JDK 21 (Temurin or your OS package).",
            image="eclipse-temurin:21-jre", example="-jar app.jar"),
    Runtime("dotnet", ".NET", ("dotnet",), env=(("DOTNET_CLI_HOME", "{home}"), ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
                                                ("DOTNET_NOLOGO", "1"), ("NUGET_PACKAGES", "{home}/.nuget")),
            install="Install the .NET 8 SDK or runtime: https://dot.net", image="mcr.microsoft.com/dotnet/runtime:8.0",
            example="app.dll"),
    Runtime("rust", "Rust (cargo)", ("cargo",), env=(("CARGO_HOME", "{home}/.cargo"), ("CARGO_TARGET_DIR", "target")),
            install="Install Rust with rustup: https://rustup.rs", image="rust:1-slim", example="run --release"),
    Runtime("ruby", "Ruby", ("ruby",), env=(("GEM_HOME", "{home}/.gem"),), install="Install Ruby 3.2 or newer.",
            image="ruby:3.3-slim", example="app.rb"),
    Runtime("powershell", "PowerShell", ("pwsh",), ("-NoLogo", "-NoProfile", "-NonInteractive", "-File"),
            install="Install PowerShell 7: https://aka.ms/powershell", image="mcr.microsoft.com/powershell:lts",
            example="job.ps1"),
    Runtime("binary", "Program in the folder", (), example="./server --port 8000"),
)}
KEYS = tuple(RUNTIMES)

def search_dirs() -> tuple[str, ...]:
    """Where runtimes are found: the trusted system folders plus each runtime's usual per-user install folder.
    A file there still has to pass hostenv's checks (regular file, owned by you or root, not writable by others)."""
    home = Path.home()
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        dirs = [rf"{pf}\nodejs", rf"{pf}\dotnet", rf"{pf}\Go\bin", rf"{pf}\PowerShell\7", rf"{local}\Programs\Python\Python313",
                rf"{local}\Programs\Python\Python312", str(home / ".cargo" / "bin"), str(home / ".bun" / "bin"),
                str(home / ".deno" / "bin")]
        for base in (Path(pf) / "Eclipse Adoptium", Path(pf) / "Java", Path(pf) / "Microsoft"):
            if base.is_dir():
                dirs += [str(p / "bin") for p in sorted(base.glob("jdk*"), reverse=True)]
        dirs += [str(p / "bin") for p in sorted(Path("C:/").glob("Ruby*"), reverse=True)]
        return tuple(dirs)
    return ("/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/go/bin", "/snap/bin",
            str(home / ".cargo" / "bin"), str(home / ".bun" / "bin"), str(home / ".deno" / "bin"),
            str(home / ".dotnet"), str(home / ".local" / "bin"), "/opt/homebrew/opt/openjdk/bin")

def find_all() -> tuple[tuple[str, str], ...]:
    """(runtime, executable) for every runtime found on this machine."""
    from pythonator.safe.hostenv import _safe_file

    exts = (".exe", ".cmd") if sys.platform == "win32" else ("",)
    out = []
    for r in RUNTIMES.values():
        hit = next((str(p) for name in r.exe for d in search_dirs() for ext in exts if _safe_file(p := Path(d, name + ext))), "")
        if hit:
            out.append((r.key, hit))
    return tuple(out)

def rustup_home(exe: str) -> str:
    """rustup's cargo in ~/.cargo/bin is a proxy that runs the toolchain from ~/.rustup (no credentials there), which
    the jail must read; ~/.cargo itself stays hidden."""
    import ntpath
    import posixpath

    m = ntpath if ("\\" in exe or exe[1:2] == ":") else posixpath
    d = m.dirname(exe)
    if m.basename(d) == "bin" and m.basename(m.dirname(d)) == ".cargo":
        return m.join(m.dirname(m.dirname(d)), ".rustup")
    return ""

def install_root(exe: str) -> str:
    """The folder to make visible (read-only): the prefix above bin/ (or Scripts/), else the exe's own folder.
    Never a dot-folder parent: ~/.cargo holds credentials.toml and ~/.local holds keyrings, so their bin/ only."""
    import ntpath
    import posixpath

    m = ntpath if ("\\" in exe or exe[1:2] == ":") else posixpath
    d = m.dirname(exe)
    if m.basename(d).lower() not in ("bin", "scripts"):
        return d
    parent = m.dirname(d)
    return d if m.basename(parent).startswith(".") else parent
