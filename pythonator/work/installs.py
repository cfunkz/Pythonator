"""Python runtimes owned by Pythonator, installed and found with uv (bundled as a Python package dependency).
A service that asks for `runtime_version: "3.13"` runs on that exact interpreter from `<data>/runtimes/python`,
not whatever happens to be on PATH. uv downloads python-build-standalone builds and checks their hashes."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from pythonator.safe.hostenv import host_env
from pythonator.safe.process import NO_WINDOW

VERSION_OK = frozenset("0123456789.")

def uv_bin() -> str:
    try:
        from uv import find_uv_bin

        return str(find_uv_bin())
    except (ImportError, FileNotFoundError):
        return shutil.which("uv", path=str(Path(sys.executable).parent)) or ""

class Installs:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "runtimes" / "python"
        self.found: dict[str, str] = {}

    def _env(self) -> dict[str, str]:
        return host_env({"UV_PYTHON_INSTALL_DIR": str(self.dir), "UV_PYTHON_PREFERENCE": "only-managed", "UV_NO_PROGRESS": "1"})

    async def _uv(self, *args: str, timeout: float = 600) -> tuple[int, str]:
        uv = uv_bin()
        if not uv:
            return 127, "uv isn't installed with Pythonator; reinstall it"
        p = await asyncio.create_subprocess_exec(uv, *args, env=self._env(), stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **NO_WINDOW)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except TimeoutError:
            p.kill()
            return 124, "uv took too long"
        return p.returncode or 0, out.decode("utf-8", "replace").strip()

    async def find(self, version: str) -> str:
        """The interpreter for `version` ("3.13"), or "" when it isn't installed. Cached per run of the service."""
        if not version or not set(version) <= VERSION_OK:
            return ""
        if version in self.found and Path(self.found[version]).exists():
            return self.found[version]
        code, out = await self._uv("python", "find", "--no-project", version, timeout=30)
        path = out.splitlines()[-1].strip() if code == 0 and out else ""
        if path and Path(path).is_file():
            self.found[version] = path
            return path
        return ""

    async def install(self, version: str) -> tuple[bool, str]:
        if not version or not set(version) <= VERSION_OK:
            return False, "A version is digits and dots, for example 3.13."
        self.dir.mkdir(parents=True, exist_ok=True)
        code, out = await self._uv("python", "install", version)
        return code == 0, out[-400:] or ("Installed." if code == 0 else "uv failed")

    async def listing(self) -> list[dict[str, str]]:
        code, out = await self._uv("python", "list", "--only-installed", "--output-format", "json", timeout=30)
        if code != 0:
            return []
        import json

        try:
            return [{"version": x.get("version", ""), "path": x.get("path") or ""} for x in json.loads(out)]
        except (ValueError, AttributeError):
            return []
