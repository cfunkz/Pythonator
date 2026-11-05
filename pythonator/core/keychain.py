"""Secrets in the operating system's keychain: Windows Credential Manager, the macOS Keychain, or the Secret Service
(GNOME Keyring, KWallet) on Linux. Standard library only.

Used for plugin settings of kind ``secret`` (API keys, tokens), so they never sit in ``settings.json``. Where no
keychain works (a Linux server without a Secret Service, for example) :class:`Vault` keeps them in a separate file
that only you can read, and says so.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)
SERVICE = "Pythonator"
MAX_SECRET = 8192


class KeychainError(Exception):
    """The keychain refused or isn't there."""


def _check(account: str, secret: str | None = None) -> None:
    if not account or len(account) > 200 or not account.isprintable():
        raise KeychainError("bad account name")
    if secret is not None and (len(secret) > MAX_SECRET or "\0" in secret):
        raise KeychainError("A secret is at most 8 KB of text")


# -- Windows: Credential Manager --------------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as w

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR), ("Comment", w.LPWSTR),
                    ("LastWritten", w.FILETIME), ("CredentialBlobSize", w.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", w.DWORD),
                    ("AttributeCount", w.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", w.LPWSTR),
                    ("UserName", w.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _CredWrite = _advapi.CredWriteW
    _CredWrite.argtypes = [ctypes.POINTER(_CREDENTIAL), w.DWORD]
    _CredWrite.restype = w.BOOL
    _CredRead = _advapi.CredReadW
    _CredRead.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _CredRead.restype = w.BOOL
    _CredDelete = _advapi.CredDeleteW
    _CredDelete.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
    _CredDelete.restype = w.BOOL
    _CredFree = _advapi.CredFree
    _CredFree.argtypes = [ctypes.c_void_p]
    _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE, _ERROR_NOT_FOUND = 1, 2, 1168

    def _target(account: str) -> str:
        return f"{SERVICE}:{account}"

    def _win_set(account: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        cred = _CREDENTIAL(Type=_CRED_TYPE_GENERIC, TargetName=_target(account), CredentialBlobSize=len(blob),
                           CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)),
                           Persist=_CRED_PERSIST_LOCAL_MACHINE, UserName=SERVICE)
        if not _CredWrite(ctypes.byref(cred), 0):
            raise KeychainError(f"Credential Manager refused it ({ctypes.get_last_error()})")

    def _win_get(account: str) -> str | None:
        ptr = ctypes.POINTER(_CREDENTIAL)()
        if not _CredRead(_target(account), _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return None
            raise KeychainError(f"Credential Manager refused it ({ctypes.get_last_error()})")
        try:
            size = ptr.contents.CredentialBlobSize
            return ctypes.string_at(ptr.contents.CredentialBlob, size).decode("utf-16-le")
        finally:
            _CredFree(ptr)

    def _win_delete(account: str) -> None:
        if not _CredDelete(_target(account), _CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != _ERROR_NOT_FOUND:
            raise KeychainError(f"Credential Manager refused it ({ctypes.get_last_error()})")


# -- macOS: the Keychain, through Security.framework ------------------------------------------------------------


def _mac() -> object:
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("Security") or "/System/Library/Frameworks/Security.framework/Security"
    sec = ctypes.CDLL(path)
    u32, vp, cp = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p
    sec.SecKeychainAddGenericPassword.argtypes = [vp, u32, cp, u32, cp, u32, cp, ctypes.POINTER(vp)]
    sec.SecKeychainFindGenericPassword.argtypes = [vp, u32, cp, u32, cp, ctypes.POINTER(u32), ctypes.POINTER(vp),
                                                   ctypes.POINTER(vp)]
    sec.SecKeychainItemModifyAttributesAndData.argtypes = [vp, vp, u32, cp]
    sec.SecKeychainItemFreeContent.argtypes = [vp, vp]
    sec.SecKeychainItemDelete.argtypes = [vp]
    for fn in ("SecKeychainAddGenericPassword", "SecKeychainFindGenericPassword",
               "SecKeychainItemModifyAttributesAndData", "SecKeychainItemFreeContent", "SecKeychainItemDelete"):
        getattr(sec, fn).restype = ctypes.c_int32
    return sec


_MAC_NOT_FOUND = -25300


def _mac_find(account: str) -> tuple[int, bytes | None, object]:
    import ctypes

    sec = _mac()
    svc, acct = SERVICE.encode(), account.encode()
    length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    status = sec.SecKeychainFindGenericPassword(None, len(svc), svc, len(acct), acct, ctypes.byref(length),  # type: ignore[attr-defined]
                                                ctypes.byref(data), ctypes.byref(item))
    if status != 0:
        return status, None, None
    value = ctypes.string_at(data, length.value)
    sec.SecKeychainItemFreeContent(None, data)  # type: ignore[attr-defined]
    return 0, value, item


def _mac_set(account: str, secret: str) -> None:
    import ctypes

    sec = _mac()
    blob = secret.encode()
    status, _old, item = _mac_find(account)
    if status == 0:
        status = sec.SecKeychainItemModifyAttributesAndData(item, None, len(blob), blob)  # type: ignore[attr-defined]
        _release(item)
    elif status == _MAC_NOT_FOUND:
        svc, acct = SERVICE.encode(), account.encode()
        status = sec.SecKeychainAddGenericPassword(None, len(svc), svc, len(acct), acct, len(blob), blob,  # type: ignore[attr-defined]
                                                   ctypes.POINTER(ctypes.c_void_p)())
    if status != 0:
        raise KeychainError(f"the Keychain refused it (OSStatus {status})")


def _mac_get(account: str) -> str | None:
    status, value, item = _mac_find(account)
    _release(item)
    if status == _MAC_NOT_FOUND:
        return None
    if status != 0 or value is None:
        raise KeychainError(f"the Keychain refused it (OSStatus {status})")
    return value.decode("utf-8", "replace")


def _mac_delete(account: str) -> None:
    status, _value, item = _mac_find(account)
    if status == 0:
        status = _mac().SecKeychainItemDelete(item)  # type: ignore[attr-defined]
        _release(item)
    if status not in (0, _MAC_NOT_FOUND):
        raise KeychainError(f"the Keychain refused it (OSStatus {status})")


def _release(item: object) -> None:
    if item:
        import ctypes
        import ctypes.util

        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or "")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        with contextlib.suppress(Exception):
            cf.CFRelease(item)


# -- Linux: the Secret Service, through secret-tool (libsecret) -------------------------------------------------


def _secret_tool(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    tool = shutil.which("secret-tool")
    if not tool or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise KeychainError("no Secret Service here (secret-tool and a desktop session are needed)")
    try:
        return subprocess.run([tool, *args], input=stdin, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeychainError(f"secret-tool failed: {exc}") from exc


def _linux_set(account: str, secret: str) -> None:
    # The secret goes on stdin, never on the command line where other programs could see it.
    done = _secret_tool("store", f"--label={SERVICE} {account}", "service", SERVICE, "account", account,
                        stdin=secret)
    if done.returncode != 0:
        raise KeychainError(done.stderr.strip() or "secret-tool store failed")


def _linux_get(account: str) -> str | None:
    done = _secret_tool("lookup", "service", SERVICE, "account", account)
    if done.returncode != 0:
        if done.stderr.strip():
            raise KeychainError(done.stderr.strip())
        return None
    return done.stdout


def _linux_delete(account: str) -> None:
    _secret_tool("clear", "service", SERVICE, "account", account)


# -- the public functions ---------------------------------------------------------------------------------------


def set_secret(account: str, secret: str) -> None:
    _check(account, secret)
    if sys.platform == "win32":
        _win_set(account, secret)
    elif sys.platform == "darwin":
        _mac_set(account, secret)
    else:
        _linux_set(account, secret)


def get_secret(account: str) -> str | None:
    _check(account)
    if sys.platform == "win32":
        return _win_get(account)
    if sys.platform == "darwin":
        return _mac_get(account)
    return _linux_get(account)


def delete_secret(account: str) -> None:
    _check(account)
    if sys.platform == "win32":
        _win_delete(account)
    elif sys.platform == "darwin":
        _mac_delete(account)
    else:
        _linux_delete(account)


IN_KEYCHAIN = "@keychain"  # what settings.json holds in place of a secret
IN_FILE = "@private-file"


class Vault:
    """Secrets by account: the OS keychain, or ``secrets.json`` (readable by you only) where there is none.

    ``use_keychain=False`` (tests) always uses the file.
    """

    def __init__(self, file: Path, *, use_keychain: bool = True) -> None:
        self.file = file
        self.use_keychain = use_keychain and os.environ.get("PYTHONATOR_NO_KEYCHAIN") != "1"

    def store(self, account: str, secret: str) -> str:
        """Keep ``secret``; returns the marker to save in its place (:data:`IN_KEYCHAIN` or :data:`IN_FILE`)."""
        _check(account, secret)
        if self.use_keychain:
            try:
                set_secret(account, secret)
                if get_secret(account) == secret:
                    self._file_delete(account)
                    return IN_KEYCHAIN
            except (KeychainError, OSError) as exc:
                log.warning("no usable keychain (%s); keeping %s in %s", exc, account, self.file.name)
        data = self._file_read()
        data[account] = secret
        self._file_write(data)
        return IN_FILE

    def load(self, account: str, marker: str) -> str:
        if marker == IN_KEYCHAIN:
            try:
                return get_secret(account) or ""
            except (KeychainError, OSError) as exc:
                log.warning("can't read %s from the keychain: %s", account, exc)
                return ""
        if marker == IN_FILE:
            return str(self._file_read().get(account, ""))
        return marker  # a value saved before secrets moved to the keychain

    def delete(self, account: str) -> None:
        if self.use_keychain:
            with contextlib.suppress(KeychainError, OSError):
                delete_secret(account)
        self._file_delete(account)

    # -- the file fallback --

    def _file_read(self) -> dict[str, str]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _file_write(self, data: dict[str, str]) -> None:
        from ..paths import make_private

        self.file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".secrets.", dir=self.file.parent)
        try:
            make_private(Path(tmp))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            Path(tmp).replace(self.file)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

    def _file_delete(self, account: str) -> None:
        data = self._file_read()
        if account in data:
            del data[account]
            self._file_write(data)
