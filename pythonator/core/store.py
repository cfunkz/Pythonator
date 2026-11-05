"""Reading and writing the services and settings files: atomic, private, and never losing a file it can't read."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..paths import DataDirs, make_private
from .models import Service, ServiceFile, Settings

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)
MAX_FILE = 16 * 1024 * 1024


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step: a crash leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        make_private(Path(tmp))
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f"{path.name} is larger than 16 MB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _set_aside(path: Path) -> None:
    """Keep a file that couldn't be read whole, so saving doesn't destroy it."""
    with contextlib.suppress(OSError):
        path.replace(path.with_suffix(path.suffix + ".bad"))
    log.warning("%s couldn't be read whole; kept as %s.bad", path.name, path.name)


class Store:
    def __init__(self, dirs: DataDirs) -> None:
        self.dirs = dirs
        self.problems: list[str] = []  # shown once in the window

    def load_services(self) -> list[Service]:
        path = self.dirs.services_file
        try:
            raw = _read_json(path)
        except (OSError, ValueError) as exc:
            self.problems.append(f"Couldn't read {path.name}: {exc}")
            _set_aside(path)
            return []
        if raw is None:
            return import_old(self.dirs)
        items = raw.get("services", []) if isinstance(raw, dict) else []
        services: list[Service] = []
        bad = 0
        for item in items if isinstance(items, list) else []:
            try:
                services.append(Service.model_validate(item))
            except ValidationError as exc:
                bad += 1
                self.problems.append(f"Skipped a service in {path.name}: {exc.errors()[0]['msg']}")
        if bad:
            _set_aside(path)
        unique: dict[str, Service] = {}
        for service in services:
            unique.setdefault(service.id, service)
        return list(unique.values())

    def save_services(self, services: list[Service]) -> None:
        write_atomic(self.dirs.services_file, ServiceFile(services=services).model_dump_json(indent=2))

    def load_settings(self) -> Settings:
        path = self.dirs.settings_file
        try:
            raw = _read_json(path)
            return Settings.model_validate(raw or {})
        except (OSError, ValueError, ValidationError) as exc:
            self.problems.append(f"Settings reset: {exc}")
            if path.is_file():
                _set_aside(path)
            return Settings()

    def save_settings(self, settings: Settings) -> None:
        write_atomic(self.dirs.settings_file, settings.model_dump_json(indent=2))


def _old_data_file() -> Path | None:
    """Pythonator 0.0.9's ``pythonator.json``."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Pythonator"
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Pythonator"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "pythonator"
    path = base / "pythonator.json"
    return path if path.is_file() else None


def import_old(dirs: DataDirs, path: Path | None = None) -> list[Service]:
    """Bring the services of Pythonator 0.0.9 and earlier over, once. Anything that doesn't validate is skipped."""
    path = path or _old_data_file()
    if path is None:
        return []
    from . import validate

    try:
        raw = _read_json(path)
    except (OSError, ValueError):
        return []
    out: list[Service] = []
    for old in (raw or {}).get("services", []) if isinstance(raw, dict) else []:
        if not isinstance(old, dict):
            continue
        try:
            target, mode = str(old.get("target", "")), str(old.get("mode", "script"))
            folder = str(old.get("workdir") or (Path(target).parent if mode == "script" and target else ""))
            runtime = str(old.get("runtime", "python"))
            data: dict[str, Any] = {
                "id": str(old.get("id", "")).lower() or None, "name": old.get("name", "Service"), "folder": folder,
                "args": validate.split_command(str(old.get("args", ""))), "env": old.get("env", {}),
                "env_file": old.get("env_file", ""), "autostart": bool(old.get("autostart")),
                "memory_mb": int(old.get("memory_limit_mb", 0) or 0),
                "mode": "container" if old.get("container") else "sandbox" if old.get("sandbox") else "full",
                "full_access_ok": not old.get("sandbox") and not old.get("container"),
            }
            if mode == "script":
                data.update(runtime=runtime if runtime != "bun" or target.endswith(".ts") else "bun",
                            entry=Path(target).name)
            else:
                data.update(runtime="custom", command=validate.split_command(target))
            if old.get("git_url"):
                data["git"] = {"url": old["git_url"], "branch": old.get("git_branch", ""),
                               "pull_on_start": bool(old.get("git_pull_on_start"))}
            if data["id"] is None:
                del data["id"]
            out.append(Service.model_validate(data))
        except (ValidationError, ValueError, TypeError):
            continue
    if out:
        log.info("Imported %d services from %s", len(out), path)
    return out
