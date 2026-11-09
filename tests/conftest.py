import os

import pytest

from pythonator.policy.host import HostFacts


@pytest.fixture
def host(tmp_path):
    return HostFacts(
        os="linux",
        home="/home/me",
        data_dir="/home/me/.local/share/Pythonator",
        uid=1000,
        bwrap="/usr/bin/bwrap",
        pasta="/usr/bin/pasta",
        systemd_run="/usr/bin/systemd-run",
        systemd_user=True,
        landlock_abi=6,
        usr_merged=True,
        python="/usr/bin/python3",
        launcher="/opt/pythonator/jail/launcher.py",
        node="",
        podman="/usr/bin/podman",
        seccomp=True,
    )


def symlink_or_skip(target, link):
    """Windows needs Developer Mode or admin rights for symlinks; skip there rather than fail."""
    try:
        os.symlink(target, link)
    except OSError as e:
        pytest.skip(f"can't make symlinks here: {e}")


@pytest.fixture(autouse=True)
def _own_runtime_dir(monkeypatch):
    """Each test gets its own short XDG_RUNTIME_DIR, so gate sockets never collide with another daemon's."""
    import shutil
    import sys
    import tempfile

    if sys.platform == "win32":
        yield
        return
    d = tempfile.mkdtemp(prefix="pz-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    yield
    shutil.rmtree(d, ignore_errors=True)
