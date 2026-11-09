"""The Windows jail's pure parts, tested on every OS. The Windows CI job also starts a real AppContainer."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("winjail", Path(__file__).parent.parent / "pythonator/jail/winjail.py")
assert SPEC and SPEC.loader
wj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wj)

ME, ALL = "S-1-15-2-111", wj.ALL_APP_PACKAGES
USERS = "S-1-5-32-545"


def ace(kind, mask, sid, flags=0):
    return (kind, flags, mask, sid)


def test_write_grant_added_once():
    assert wj.plan_access([], ME, [ALL], True) == ["grant"]
    assert wj.plan_access([ace(wj.ALLOW, wj.MODIFY, ME)], ME, [ALL], True) == []


def test_read_only_after_write_is_set_again():
    assert wj.plan_access([ace(wj.ALLOW, wj.MODIFY, ME)], ME, [ALL], False) == ["set"]


def test_read_only_denies_write_inherited_from_above():
    aces = [ace(wj.ALLOW, wj.MODIFY, ME, wj.INHERITED)]
    assert wj.plan_access(aces, ME, [ALL], False) == ["deny"]  # it can read already


def test_read_only_denies_write_every_appcontainer_has():
    aces = [ace(wj.ALLOW, wj.MODIFY, ALL)]
    assert "deny" in wj.plan_access(aces, ME, [ALL], False)


def test_program_files_needs_nothing():
    aces = [ace(wj.ALLOW, wj.READ_EXECUTE, ALL), ace(wj.ALLOW, wj.MODIFY, USERS)]
    assert wj.plan_access(aces, ME, [ALL], False) == []


def test_write_after_read_only_replaces_the_deny():
    aces = [ace(wj.DENY, wj.WRITE_RIGHTS, ME), ace(wj.ALLOW, wj.READ_EXECUTE, ME)]
    assert wj.plan_access(aces, ME, [ALL], True) == ["set"]


def test_env_block_is_sorted_and_double_nul_terminated():
    block = wj.env_block({"b": "2", "A": "1", "Path": "x"})
    assert block == "A=1\0b=2\0Path=x\0\0"


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows the launcher runs for real")
def test_launcher_refuses_off_windows():
    done = subprocess.run([sys.executable, "-I", "-S", wj.__file__, json.dumps({})], capture_output=True, text=True)
    assert done.returncode == 2 and "for Windows" in done.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_appcontainer_probe_runs_for_real():
    done = subprocess.run([sys.executable, "-I", "-S", wj.__file__, "--probe"], capture_output=True, text=True, timeout=60)
    assert json.loads(done.stdout) == {"appcontainer": True}, done.stderr
