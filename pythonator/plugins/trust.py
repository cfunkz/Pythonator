"""Which plugins are official: their files match, byte for byte, the hashes reviewed into ``official.json``.

The catalog is regenerated with ``python -m pythonator.plugins.trust --update`` and reviewed in the same pull
request as the plugin's code: that review is the approval. Hashing only reads files; nothing is imported.
Line endings are normalised, so a Windows checkout hashes the same as CI. A plugin with one byte changed, an extra
file, or a missing one is third-party.
"""

from __future__ import annotations

import hashlib
import json
import sys
from functools import cache
from pathlib import Path

from ..paths import official_plugins_dir

CATALOG = Path(__file__).with_name("official.json")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}  # never run: see worker.py
MAX_FILES = 2000
MAX_BYTES = 20 * 1024 * 1024


def tree_hash(folder: Path) -> str:
    """SHA-256 over every file's relative path and contents. Raises ValueError for links or a huge tree."""
    digest = hashlib.sha256()
    total = count = 0
    # Sorted by the path's parts, case-sensitively, so the order (and the hash) is the same on every system:
    # sorting the Path objects themselves ignores case on Windows, and README.md would move.
    for path in sorted(folder.rglob("*"), key=lambda p: p.relative_to(folder).parts):
        rel = path.relative_to(folder)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise ValueError(f"{rel} is a link; plugins may not contain links")
        if not path.is_file():
            continue
        data = path.read_bytes().replace(b"\r\n", b"\n")
        count, total = count + 1, total + len(data)
        if count > MAX_FILES or total > MAX_BYTES:
            raise ValueError("The plugin is too large (2000 files or 20 MB at most)")
        digest.update(rel.as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


@cache
def catalog() -> dict[str, str]:
    try:
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.get("plugins", {}).items()} if isinstance(data, dict) else {}


def is_official(plugin_id: str, folder: Path) -> bool:
    """Official means: shipped in the app's own plugins folder, listed in the catalog, and unchanged."""
    expected = catalog().get(plugin_id)
    if not expected or folder.resolve().parent != official_plugins_dir().resolve():
        return False
    try:
        return tree_hash(folder) == expected
    except (OSError, ValueError):
        return False


def update() -> dict[str, str]:
    """Rehash every plugin in the repository's plugins folder into the catalog (a developer tool)."""
    from .manifest import load

    out: dict[str, str] = {}
    for folder in sorted(p for p in official_plugins_dir().iterdir() if p.is_dir()):
        out[load(folder).id] = tree_hash(folder)
    CATALOG.write_text(json.dumps({"plugins": out}, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] == ["--update"]:
        for k, v in update().items():
            print(f"{k}: {v}")
    else:
        print("usage: python -m pythonator.plugins.trust --update")
