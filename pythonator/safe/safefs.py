"""The only way Pythonator reads a service folder.
Service folders are hostile input: a link can point at ~/.ssh, a FIFO can block a read forever, a huge file can
eat memory, and a path can be swapped between a check and an open. Every read here walks the path one component at
a time from a directory descriptor, refuses links and special files, never blocks, and is capped in size."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_READ = 8 * 1024 * 1024
MAX_ENTRIES = 5000

_POSIX = sys.platform != "win32"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)  # O_BINARY: Windows, no newline translation

class UnsafePath(Exception):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason

@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # "dir", "file", "link", "other"
    size: int
    mtime: float

def _parts(rel: str) -> list[str]:
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute():
        raise UnsafePath(rel, "must be relative to the service folder")
    parts = [x for x in p.parts if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise UnsafePath(rel, "'..' is not allowed")
    return parts

def _open_dir(root: str, parts: list[str]) -> int:
    fd = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for name in parts:
            try:
                nfd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePath(name, "is a link or not a folder") from None
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise

def _win_walk(root: str, parts: list[str], rel: str) -> str:
    """Windows: refuse a link, junction or any other reparse point anywhere between root and the target."""
    path = root
    for name in parts:
        path = os.path.join(path, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return path  # a new file being written; its parents were checked
        reparse = getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(st.st_mode) or reparse or os.path.isjunction(path):
            raise UnsafePath(rel, "is a link")
    return path

def read_bytes(root: str, rel: str, limit: int = MAX_READ) -> bytes:
    """Read a regular file inside `root`. Refuses links, FIFOs, devices, sockets and anything over `limit`."""
    parts = _parts(rel)
    if not parts:
        raise UnsafePath(rel, "is the folder itself")
    if not _POSIX:
        full = _win_walk(root, parts, rel)
        st = os.lstat(full)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise UnsafePath(rel, "is not a regular file under the size cap")
        with open(full, "rb") as f:
            return f.read(limit + 1)[:limit]
    dfd = _open_dir(root, parts[:-1])
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise UnsafePath(rel, "is a link") from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(rel, "is not a regular file")
            if st.st_size > limit:
                raise UnsafePath(rel, f"is larger than {limit // 1024} KB")
            chunks, total = [], 0
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                total += len(b)
                if total > limit:
                    raise UnsafePath(rel, "grew past the size cap while reading")
                chunks.append(b)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)

def read_text(root: str, rel: str, limit: int = MAX_READ) -> str:
    return read_bytes(root, rel, limit).decode("utf-8", errors="replace")

def list_dir(root: str, rel: str = "", limit: int = MAX_ENTRIES) -> list[Entry]:
    """List one folder without following links. Links are reported as kind "link" and never entered."""
    parts = _parts(rel) if rel else []
    out: list[Entry] = []
    if not _POSIX:
        base = _win_walk(root, parts, rel)
        with os.scandir(base) as it:
            for de in it:
                st = de.stat(follow_symlinks=False)
                kind = (
                    "link"
                    if de.is_symlink() or de.is_junction()
                    else "dir"
                    if de.is_dir(follow_symlinks=False)
                    else "file"
                    if de.is_file(follow_symlinks=False)
                    else "other"
                )
                out.append(Entry(de.name, kind, st.st_size, st.st_mtime))
                if len(out) >= limit:
                    break
        return sorted(out, key=lambda e: (e.kind != "dir", e.name.lower()))
    dfd = _open_dir(root, parts)
    try:
        for name in os.listdir(dfd):
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            m = st.st_mode
            kind = "dir" if stat.S_ISDIR(m) else "file" if stat.S_ISREG(m) else "link" if stat.S_ISLNK(m) else "other"
            out.append(Entry(name, kind, st.st_size, st.st_mtime))
            if len(out) >= limit:
                break
    finally:
        os.close(dfd)
    return sorted(out, key=lambda e: (e.kind != "dir", e.name.lower()))

def write_text(root: str, rel: str, text: str) -> None:
    """Write a file inside `root` (used by the editor). Refuses links; writes a temp file and renames over."""
    write_bytes(root, rel, text.encode("utf-8"))

def write_bytes(root: str, rel: str, data: bytes, make_parents: bool = False) -> None:
    """Write a file inside `root`: no link is followed anywhere on the way, a temp file is renamed over the target.
    `make_parents` creates missing folders one component at a time, also without following links."""
    parts = _parts(rel)
    if not parts:
        raise UnsafePath(rel, "is the folder itself")
    if not _POSIX:
        full = _win_walk(root, parts, rel)
        if make_parents:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            _win_walk(root, parts, rel)
        tmp = full + ".pythonator-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        return
    if make_parents:
        _make_dirs(root, parts[:-1])
    dfd = _open_dir(root, parts[:-1])
    try:
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(rel, "is not a regular file")
            mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            mode = 0o644
        tmp = f".{parts[-1]}.pythonator-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC, mode, dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)

def _make_dirs(root: str, parts: list[str]) -> None:
    fd = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for name in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=fd)
            try:
                nfd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePath(name, "is a link or not a folder") from None
                raise
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)

def unlink(root: str, rel: str) -> None:
    """Delete one regular file inside `root`; a link is refused, not followed. A file already gone is fine."""
    parts = _parts(rel)
    if not parts:
        raise UnsafePath(rel, "is the folder itself")
    if not _POSIX:
        full = _win_walk(root, parts, rel)
        with contextlib.suppress(FileNotFoundError):
            if os.path.islink(full) or os.path.isjunction(full):
                raise UnsafePath(rel, "is a link")
            os.remove(full)
        return
    dfd = _open_dir(root, parts[:-1])
    try:
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(rel, "is not a regular file")
        os.unlink(parts[-1], dir_fd=dfd)
    finally:
        os.close(dfd)
