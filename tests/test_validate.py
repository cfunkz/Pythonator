from __future__ import annotations

import pytest

from pythonator.core import validate


@pytest.mark.parametrize("bad", ["", "   ", "a" * 65, "bot\x00", "bot\u202eexe", "rm -rf /;", "bot\nnext"])
def test_name_refuses(bad: str) -> None:
    with pytest.raises(ValueError):
        validate.name(bad)


def test_name_accepts() -> None:
    assert validate.name("  My bot (v2)_x.y-z ") == "My bot (v2)_x.y-z"


@pytest.mark.parametrize("key", ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "1ABC", "A-B", "A B", ""])
def test_env_key_refuses(key: str) -> None:
    with pytest.raises(ValueError):
        validate.env_key(key)


def test_env_values() -> None:
    assert validate.env({"TOKEN": "abc"}) == {"TOKEN": "abc"}
    with pytest.raises(ValueError):
        validate.env({"A": "x\x00y"})


@pytest.mark.parametrize("bad", ["a\x00b", "\u200bhidden", "x\u2066y"])
def test_args_refuse_invisible_characters(bad: str) -> None:
    with pytest.raises(ValueError):
        validate.args([bad])


def test_command_round_trip() -> None:
    parts = validate.split_command('uvicorn app:app --host "127.0.0.1" --port 8000')
    assert parts == ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"]
    assert validate.split_command(validate.join_command(parts)) == parts


def test_ports() -> None:
    assert validate.parse_ports("8000, 8443 8000") == [8000, 8443]
    for bad in ("0", "65536", "http", "-1"):
        with pytest.raises(ValueError):
            validate.parse_ports(bad)


@pytest.mark.parametrize("url", ["https://github.com/a/b.git", "git@github.com:a/b.git", "ssh://git@host/a/b"])
def test_git_url_accepts(url: str) -> None:
    assert validate.git_url(url) == url


@pytest.mark.parametrize("url", ["--upload-pack=touch /tmp/x", "ext::sh -c id", "file:///etc", "http://x/y",
                                 "https://host/a b", "-https://x/y"])
def test_git_url_refuses(url: str) -> None:
    with pytest.raises(ValueError):
        validate.git_url(url)


@pytest.mark.parametrize("ref", ["--orphan", "a..b", "a b", "x" * 201])
def test_git_ref_refuses(ref: str) -> None:
    with pytest.raises(ValueError):
        validate.git_ref(ref)


def test_image_and_network() -> None:
    assert validate.image("python:3.13-slim") == "python:3.13-slim"
    assert validate.image("ghcr.io/owner/app:1.2@sha256:" + "a" * 64)
    for bad in ("Python", "--privileged", "a b"):
        with pytest.raises(ValueError):
            validate.image(bad)
    with pytest.raises(ValueError):
        validate.network_name("host")


def test_relative_file_stays_inside(tmp_path: object) -> None:
    from pathlib import Path

    base = Path(str(tmp_path))
    (base / "app.py").write_text("")
    assert validate.relative_file("app.py", str(base)) == "app.py"
    for bad in ("../escape.py", "/etc/passwd"):
        with pytest.raises(ValueError):
            validate.relative_file(bad, str(base))
