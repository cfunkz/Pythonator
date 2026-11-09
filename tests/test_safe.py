import os
import sys

import pytest
from conftest import symlink_or_skip

from pythonator.safe import safefs
from pythonator.safe.mask import MASK, Masker


def test_reads_regular_file(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    assert safefs.read_text(str(tmp_path), "a.txt") == "hi"


def test_refuses_link_out(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("key")
    root = tmp_path / "svc"
    root.mkdir()
    symlink_or_skip(secret, root / "link")
    with pytest.raises(safefs.UnsafePath):
        safefs.read_text(str(root), "link")


def test_refuses_dotdot_and_absolute(tmp_path):
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(safefs.UnsafePath):
            safefs.read_text(str(tmp_path), bad)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on Windows")
def test_fifo_never_blocks(tmp_path):
    os.mkfifo(tmp_path / "f")
    with pytest.raises(safefs.UnsafePath):
        safefs.read_text(str(tmp_path), "f")


def test_size_cap(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * 2048)
    with pytest.raises(safefs.UnsafePath):
        safefs.read_bytes(str(tmp_path), "big", limit=1024)


def test_write_refuses_link(tmp_path):
    symlink_or_skip(tmp_path / "elsewhere", tmp_path / "l")
    with pytest.raises(safefs.UnsafePath):
        safefs.write_text(str(tmp_path), "l", "x")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_refuses_junction_in_the_path(tmp_path):
    import _winapi  # type: ignore[import-not-found]

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("key")
    root = tmp_path / "svc"
    root.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "j"))  # no admin rights needed: the case that matters
    with pytest.raises(safefs.UnsafePath):
        safefs.read_text(str(root), "j/secret.txt")
    with pytest.raises(safefs.UnsafePath):
        safefs.list_dir(str(root), "j")
    with pytest.raises(safefs.UnsafePath):
        safefs.write_text(str(root), "j/new.txt", "x")


def test_mask_values_urls_and_tokens():
    m = Masker(["hunter2-secret"])
    assert "hunter2-secret" not in m.mask("pw=hunter2-secret")
    assert m.mask("postgres://bob:pa55word@db/x") == f"postgres://bob:{MASK}@db/x"
    assert "abcdefghijklmnop" not in m.mask("Authorization: Bearer abcdefghijklmnop")
