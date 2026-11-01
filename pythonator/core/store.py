"""Loading and saving the application config, including migration from Pythonator 1.x."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from .models import AppConfig, LaunchMode, RestartPolicy, ServiceConfig
from .safety import make_private

log = logging.getLogger(__name__)

_PYTHON_NAMES = frozenset({"python", "python3", "python.exe", "python3.exe", "py", "pythonw.exe"})


class ConfigStore:
    """Reads and atomically writes ``pythonator.json``."""

    def __init__(self, path: Path, legacy_path: Path | None = None) -> None:
        self.path = path
        self.legacy_path = legacy_path
        self.backup_path = path.with_name(path.name + ".bak")
        # Set when the config exists but can't be read (permissions, locked by another program):
        # saving then would replace the user's services with an empty list, so we don't.
        self.read_only = False
        self.recovered_from: Path | None = None  # set when the backup had to be used

    def load(self) -> AppConfig:
        if self.path.is_file():
            try:
                text = self.path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                log.error("Can't read %s (%s); changes won't be saved this session", self.path, exc)
                self.read_only = True
                return AppConfig()
            data = _parse_json(text, self.path)
            if isinstance(data, dict):
                return AppConfig.from_dict(data)
            # Corrupt (e.g. a crash or power loss mid-write by another tool). Keep it for the user,
            # then fall back to the copy made before the last successful save.
            corrupt = self.path.with_suffix(".corrupt.json")
            with contextlib.suppress(OSError):
                os.replace(self.path, corrupt)
            log.warning("Config %s was unreadable and has been moved to %s", self.path, corrupt)
            backup = _read_json(self.backup_path) if self.backup_path.is_file() else None
            if isinstance(backup, dict):
                self.recovered_from = self.backup_path
                log.warning("Restored the configuration from %s", self.backup_path)
                return AppConfig.from_dict(backup)
            return AppConfig()
        if self.legacy_path and self.legacy_path.is_file():
            legacy = _read_json(self.legacy_path)
            if isinstance(legacy, dict):
                config = migrate_legacy(legacy)
                self.save(config)
                log.info("Migrated %d services from %s", len(config.services), self.legacy_path)
                return config
        return AppConfig()

    def save(self, config: AppConfig) -> None:
        """Write atomically, keeping the previous version as ``.bak``. Raises ``OSError``."""
        if self.read_only:
            raise PermissionError(f"{self.path} could not be read, so it is not overwritten")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
        if self.path.is_file():
            with contextlib.suppress(OSError):  # best effort: the new file is written atomically anyway
                self._write_backup()
        fd, tmp = tempfile.mkstemp(prefix=".pythonator-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise


    def _write_backup(self) -> None:
        # Owner-only like the config itself (mkstemp): service env vars often hold tokens.
        data = self.path.read_bytes()
        fd = os.open(self.backup_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        make_private(self.backup_path)  # a .bak written by an older version may be world-readable
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None
    return _parse_json(text, path)


def _parse_json(text: str, path: Path) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        log.warning("Could not parse %s: %s", path, exc)
        return None


def migrate_legacy(data: dict[str, Any]) -> AppConfig:
    """Convert the 1.x ``bots.json`` mapping (``{name: {entry, flags, ...}}``) to the new model."""
    services: list[ServiceConfig] = []
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        entry = str(raw.get("entry") or "")
        flags = str(raw.get("flags") or "")
        svc = ServiceConfig(
            name=str(raw.get("name") or name),
            python=str(raw.get("python_path") or ""),
            requirements=str(raw.get("reqs") or ""),
            restart=RestartPolicy.NEVER,  # 1.x only restarted when a global checkbox was ticked
        )
        if entry:
            svc.workdir = str(Path(entry).parent)
        if not raw.get("custom_cmd"):
            svc.mode, svc.target, svc.args = LaunchMode.SCRIPT, entry, flags
            svc.workdir = ""  # script mode defaults to the script's folder anyway
        else:
            try:
                parts = shlex.split(flags)
            except ValueError:
                parts = flags.split()
            if parts and Path(parts[0]).name.lower() in _PYTHON_NAMES:
                parts = parts[1:]
                if len(parts) >= 2 and parts[0] == "-m":
                    svc.mode, svc.target, svc.args = LaunchMode.MODULE, parts[1], shlex.join(parts[2:])
                elif parts:
                    svc.mode, svc.target, svc.args = LaunchMode.SCRIPT, parts[0], shlex.join(parts[1:])
            elif parts:
                # 1.x ran ``python -m <first word>`` for custom commands.
                svc.mode, svc.target, svc.args = LaunchMode.MODULE, parts[0], shlex.join(parts[1:])
        services.append(svc)
    return AppConfig(services=services)
