"""Starter dependency files, for projects that don't have one yet.

"📦 Dependencies → Create package.json" (or go.mod, Gemfile, ...) writes one of these into the
working directory and opens it in the editor; "Install dependencies" then installs what it lists.
Templates are written locally, so this works offline and doesn't need the runtime installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import LaunchMode, Runtime, ServiceConfig


@dataclass(frozen=True, slots=True)
class Starter:
    name: str  # file name, e.g. "package.json"
    content: str


def project_name(workdir: Path) -> str:
    """A package-manager-safe name from the folder name: lowercase, dashes, never empty."""
    name = re.sub(r"[^a-z0-9._-]+", "-", workdir.name.lower()).strip("-._")
    return name or "app"


def _script(cfg: ServiceConfig, workdir: Path) -> str | None:
    """The service's script relative to its working dir (forward slashes), if it runs one."""
    if cfg.mode is not LaunchMode.SCRIPT or not cfg.target.strip():
        return None
    path = Path(cfg.target)
    try:
        return (path.relative_to(workdir) if path.is_absolute() else path).as_posix()
    except ValueError:
        return path.as_posix()


def _json(data: object) -> str:
    return json.dumps(data, indent=2) + "\n"


def starter_for(cfg: ServiceConfig, workdir: Path) -> Starter | None:
    """A starter dependency file for ``cfg``'s runtime, or None if the runtime has none."""
    name, script = project_name(workdir), _script(cfg, workdir)
    match cfg.runtime:
        case Runtime.PYTHON:
            return Starter("requirements.txt", "# One package per line, e.g.\n# requests>=2.31\n# discord.py==2.4.0\n")
        case Runtime.NODE | Runtime.BUN:
            run = "bun run" if cfg.runtime is Runtime.BUN else "node"
            scripts = {"start": f"{run} {script}"} if script else {}
            return Starter("package.json", _json({"name": name, "version": "1.0.0", "private": True,
                                                  "scripts": scripts, "dependencies": {}}))
        case Runtime.DENO:
            tasks = {"start": f"deno run -A {script}"} if script else {}
            return Starter("deno.json", _json({"tasks": tasks, "imports": {}}))
        case Runtime.GO:
            # `go mod tidy` (Install dependencies) adds a require line for every package the code imports.
            return Starter("go.mod", f"module {name}\n\ngo 1.21\n")
        case Runtime.RUBY:
            return Starter("Gemfile", 'source "https://rubygems.org"\n\n# gem "discordrb"\n# gem "sinatra", "~> 4.0"\n')
        case Runtime.PHP:
            return Starter("composer.json", _json({"require": {}}))
        case _:
            return None  # Java and .NET projects come from their build tools; scripts have no packages
