"""
Auto-updater for Pythonator using GitHub Releases.

Flow:
1. Check GitHub API for latest release
2. Compare version with current
3. Download release zip to temp
4. Launch update_launcher.pyw
5. Exit main app
6. Launcher waits, extracts, restarts

User data (bots.json, logs/) is preserved - only app files are replaced.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from PyQt6.QtCore import QThread, pyqtSignal

from config import APP_DIR

# =============================================================================
# Configuration - CHANGE THESE FOR YOUR REPO
# =============================================================================

GITHUB_OWNER = "cfunkz"             # GitHub username or org
GITHUB_REPO = "pythonator"          # Repository name
CURRENT_VERSION = "0.0.1"           # Must match __init__.py

# Set to False to disable update checking entirely
UPDATES_ENABLED = True

# Files/folders to preserve during update (relative to APP_DIR)
PRESERVE_PATHS = [
    "bots.json",
    "logs",
    "icon.ico",  # User might customize
]

# =============================================================================
# Data structures
# =============================================================================

@dataclass
class ReleaseInfo:
    """GitHub release information."""
    version: str
    download_url: str
    release_notes: str
    published_at: str
    
    @property
    def is_newer(self) -> bool:
        """Check if this release is newer than current."""
        return compare_versions(self.version, CURRENT_VERSION) > 0


def compare_versions(v1: str, v2: str) -> int:
    """
    Compare semantic versions.
    Returns: >0 if v1 > v2, <0 if v1 < v2, 0 if equal
    """
    def parse(v: str) -> tuple[int, ...]:
        # Strip 'v' prefix and any suffix like '-beta'
        v = v.lstrip('vV').split('-')[0]
        return tuple(int(x) for x in v.split('.') if x.isdigit())
    
    p1, p2 = parse(v1), parse(v2)
    # Pad to same length
    max_len = max(len(p1), len(p2))
    p1 = p1 + (0,) * (max_len - len(p1))
    p2 = p2 + (0,) * (max_len - len(p2))
    
    for a, b in zip(p1, p2):
        if a > b:
            return 1
        if a < b:
            return -1
    return 0


# =============================================================================
# GitHub API
# =============================================================================

def check_for_update(timeout: float = 10.0) -> Optional[ReleaseInfo]:
    """
    Check GitHub for latest release.
    
    Returns ReleaseInfo if update available, None otherwise.
    Raises exception on network error.
    """
    if not UPDATES_ENABLED:
        return None
    
    # Check if repo is configured
    if GITHUB_OWNER in ("your-username", "") or GITHUB_REPO in ("your-repo", ""):
        return None
    
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    
    req = Request(api_url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"Pythonator/{CURRENT_VERSION}"
    })
    
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            # No releases yet, or repo not found - not an error
            return None
        raise URLError(f"HTTP {e.code}: {e.reason}")
    
    version = data.get("tag_name", "").lstrip("vV")
    if not version:
        return None
    
    # Find the zip asset (prefer the release zip over source)
    download_url = None
    for asset in data.get("assets", []):
        name = asset.get("name", "").lower()
        if name.endswith(".zip") and "source" not in name:
            download_url = asset.get("browser_download_url")
            break
    
    # Fall back to source zipball
    if not download_url:
        download_url = data.get("zipball_url")
    
    if not download_url:
        return None
    
    release = ReleaseInfo(
        version=version,
        download_url=download_url,
        release_notes=data.get("body", ""),
        published_at=data.get("published_at", "")
    )
    
    return release if release.is_newer else None


def download_release(
    release: ReleaseInfo,
    dest_dir: Path,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    timeout: float = 60.0
) -> Path:
    """
    Download release zip to destination directory.
    
    Args:
        release: Release info with download URL
        dest_dir: Directory to save zip file
        progress_callback: Optional callback(downloaded_bytes, total_bytes)
        timeout: Download timeout in seconds
    
    Returns:
        Path to downloaded zip file
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"pythonator-{release.version}.zip"
    
    req = Request(release.download_url, headers={
        "User-Agent": f"Pythonator/{CURRENT_VERSION}"
    })
    
    with urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        
        with open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)
    
    return zip_path


# =============================================================================
# Update application
# =============================================================================

def prepare_update(zip_path: Path) -> Path:
    """
    Extract update to staging directory.
    
    Returns path to extracted app directory.
    """
    staging = zip_path.parent / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(staging)
    
    # GitHub zipball creates a nested directory - find the actual app
    contents = list(staging.iterdir())
    if len(contents) == 1 and contents[0].is_dir():
        return contents[0]
    return staging


def launch_updater(staging_path: Path) -> bool:
    """
    Launch the update launcher script and exit.
    
    The launcher will:
    1. Wait for this process to exit
    2. Copy new files over old (preserving user data)
    3. Restart the application
    """
    # Find the launcher script
    launcher = APP_DIR / "update_launcher.pyw"
    if not launcher.exists():
        # Try in the staging directory
        launcher = staging_path / "update_launcher.pyw"
    
    if not launcher.exists():
        raise FileNotFoundError("update_launcher.pyw not found")
    
    # Build command
    python = sys.executable
    args = [
        python,
        str(launcher),
        str(staging_path),      # Source: extracted update
        str(APP_DIR),           # Dest: current app directory
        str(os.getpid()),       # PID to wait for
        ",".join(PRESERVE_PATHS)  # Files to preserve
    ]
    
    # Launch detached
    if os.name == "nt":
        # Windows: use CREATE_NEW_PROCESS_GROUP and DETACHED_PROCESS
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True
        )
    else:
        # Unix: double-fork via nohup
        subprocess.Popen(
            ["nohup"] + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
    
    return True


# =============================================================================
# Qt Worker Thread
# =============================================================================

class UpdateChecker(QThread):
    """Background thread for checking updates."""
    
    # Signals
    update_available = pyqtSignal(object)  # ReleaseInfo
    no_update = pyqtSignal()
    error = pyqtSignal(str)
    not_configured = pyqtSignal()  # Updates not configured
    
    def run(self) -> None:
        if not UPDATES_ENABLED:
            self.not_configured.emit()
            return
            
        if GITHUB_OWNER in ("your-username", "") or GITHUB_REPO in ("your-repo", ""):
            self.not_configured.emit()
            return
        
        try:
            release = check_for_update()
            if release:
                self.update_available.emit(release)
            else:
                self.no_update.emit()
        except URLError as e:
            self.error.emit(f"Network error: {e.reason}")
        except Exception as e:
            self.error.emit(str(e))


class UpdateDownloader(QThread):
    """Background thread for downloading updates."""
    
    # Signals
    progress = pyqtSignal(int, int)  # downloaded, total
    finished = pyqtSignal(str)       # staging path
    error = pyqtSignal(str)
    
    def __init__(self, release: ReleaseInfo, parent=None):
        super().__init__(parent)
        self.release = release
    
    def run(self) -> None:
        try:
            # Download to temp directory
            temp_dir = Path(tempfile.gettempdir()) / "pythonator_update"
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir()
            
            zip_path = download_release(
                self.release,
                temp_dir,
                progress_callback=lambda d, t: self.progress.emit(d, t)
            )
            
            staging = prepare_update(zip_path)
            self.finished.emit(str(staging))
            
        except Exception as e:
            self.error.emit(str(e))
