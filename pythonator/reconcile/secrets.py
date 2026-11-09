"""Secrets live in the OS keychain (Secret Service, Keychain, Credential Manager), never in a spec file."""

from __future__ import annotations

KEYRING_SERVICE = "pythonator"

def _kr():  # imported lazily: keyring loads backends on import
    import keyring

    return keyring

def get(service: str, key: str) -> str | None:
    try:
        return _kr().get_password(KEYRING_SERVICE, f"{service}/{key}")
    except Exception:  # noqa: BLE001 - no keychain on this machine is a normal state
        return None

def put(service: str, key: str, value: str) -> None:
    _kr().set_password(KEYRING_SERVICE, f"{service}/{key}", value)

def delete(service: str, key: str) -> None:
    try:
        _kr().delete_password(KEYRING_SERVICE, f"{service}/{key}")
    except Exception:  # noqa: BLE001, S110 - deleting a secret that isn't there is fine
        pass
