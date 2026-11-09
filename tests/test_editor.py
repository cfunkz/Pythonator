"""Editor backend: file access through safefs, .env masking, exact-text edits, quick-fix proposals, the jailed linter,
and net.connect (host-proxied HTTPS for plugins)."""

import asyncio
import shutil
import sys

import pytest
from conftest import symlink_or_skip

from pythonator.plugins import netproxy
from pythonator.plugins.model import BadPlugin, load_manifest
from pythonator.policy.compile import compile_tool
from pythonator.policy.plan import Refused
from pythonator.work import files


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://api.anthropic.com/v1/messages", "NET-PLG-001"),
        ("https://user:pw@api.anthropic.com/", "NET-PLG-002"),
        ("https://api.anthropic.com:8443/", "NET-PLG-003"),
        ("https://169.254.169.254/latest/meta-data", "NET-PLG-004"),
        ("https://127.0.0.1/", "NET-PLG-004"),
        ("https://[::1]/", "NET-PLG-004"),
        ("https://evil.example.com/", "NET-PLG-005"),
        ("https://api.anthropic.com.evil.example.com/", "NET-PLG-005"),
    ],
)
def test_net_refuses(url, code):
    with pytest.raises(netproxy.NetRefused) as e:
        netproxy.check_url(url, ["api.anthropic.com"])
    assert e.value.code == code


def test_net_allows_granted_host_only():
    assert netproxy.check_url("https://API.anthropic.com/v1/messages?x=1", ["api.anthropic.com"]) == (
        "api.anthropic.com", "/v1/messages?x=1")


def test_secret_slots_are_filled_only_for_declared_keys():
    keys = {"api_key": "sk-real"}
    out = netproxy.fill_secrets({"x-api-key": "{secret:api_key}", "Host": "evil", "Proxy-Authorization": "x"},
                                ("api_key",), keys.get)
    assert out == {"x-api-key": "sk-real"}
    with pytest.raises(netproxy.NetRefused, match="isn't a secret this plugin declared"):
        netproxy.fill_secrets({"a": "{secret:other}"}, ("api_key",), keys.get)
    with pytest.raises(netproxy.NetRefused, match="line breaks"):
        netproxy.fill_secrets({"a": "x\r\nHost: evil"}, ("api_key",), keys.get)
    with pytest.raises(netproxy.NetRefused, match="Set the plugin"):
        netproxy.fill_secrets({"a": "{secret:api_key}"}, ("api_key",), {}.get)


def test_resolve_refuses_names_that_point_inside(monkeypatch):
    monkeypatch.setattr(netproxy.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    with pytest.raises(netproxy.NetRefused) as e:
        netproxy.resolve("api.anthropic.com")
    assert e.value.code == "NET-PLG-007"


def test_manifest_hosts(tmp_path):
    def make(extra):
        (tmp_path / "plugin.toml").write_text(f'[plugin]\nid = "x1"\npermissions = ["net.connect"]\n{extra}\n')
        (tmp_path / "main.py").write_text("")
        return load_manifest(tmp_path)

    assert make('hosts = ["api.anthropic.com"]').hosts == ("api.anthropic.com",)
    for bad in ('hosts = ["*.example.com"]', 'hosts = ["10.0.0.1"]', 'hosts = ["localhost"]', 'hosts = ["a.com:8080"]', ""):
        with pytest.raises(BadPlugin):
            make(bad)


def test_env_files_are_masked_and_read_only(tmp_path):
    (tmp_path / ".env").write_text("# db\nDATABASE_URL=postgres://u:pw@db/x\nexport TOKEN = abc\nEMPTY=\n")
    r = files.read(str(tmp_path), ".env")
    assert "pw" not in r["text"] and "abc" not in r["text"] and r["readonly"]
    assert r["text"].splitlines()[0] == "# db" and r["text"].splitlines()[3] == "EMPTY="
    with pytest.raises(files.FileRefused) as e:
        files.write(str(tmp_path), ".env", "X=1\n", r["hash"])
    assert e.value.code == "FILE-006"
    assert [x["masked"] for x in files.tree(str(tmp_path)) if x["path"] == ".env"] == [True]


def test_write_refuses_stale_hash_and_escapes(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    h = files.read(str(tmp_path), "a.py")["hash"]
    (tmp_path / "a.py").write_text("x = 2\n")
    with pytest.raises(files.FileRefused) as e:
        files.write(str(tmp_path), "a.py", "x = 3\n", h)
    assert e.value.code == "FILE-007"
    for rel in ("../out.py", "/etc/passwd", "a/../../b"):
        with pytest.raises(files.FileRefused):
            files.write(str(tmp_path), rel, "", "")


def test_links_are_refused(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "svc").mkdir()
    symlink_or_skip(str(outside), str(tmp_path / "svc" / "link.txt"))
    with pytest.raises(files.FileRefused):
        files.read(str(tmp_path / "svc"), "link.txt")
    with pytest.raises(files.FileRefused):
        files.write(str(tmp_path / "svc"), "link.txt", "x", "")
    assert outside.read_text() == "secret"
    assert all(x["path"] != "link.txt" for x in files.tree(str(tmp_path / "svc")))


def test_exact_text_edit(tmp_path):
    (tmp_path / "a.py").write_bytes(b"a = 1\nb = 2\na = 1\n")
    with pytest.raises(files.FileRefused, match="2 times"):
        files.apply_edit(str(tmp_path), "a.py", "a = 1\n", "a = 3\n")
    with pytest.raises(files.FileRefused, match="isn't in the file"):
        files.apply_edit(str(tmp_path), "a.py", "c = 1\n", "")
    files.apply_edit(str(tmp_path), "a.py", "b = 2\n", "b = 5\n")
    assert (tmp_path / "a.py").read_bytes() == b"a = 1\nb = 5\na = 1\n"
    (tmp_path / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")  # a Windows file keeps its line endings
    files.apply_edit(str(tmp_path), "w.py", "a = 1\nb = 2\n", "a = 3\nb = 2\n")
    assert (tmp_path / "w.py").read_bytes() == b"a = 3\r\nb = 2\r\n"


def test_quick_fix_becomes_one_exact_edit():
    pytest.importorskip("PyQt6.Qsci")
    from pythonator.ui.editor import one_edit

    before = "import os\nimport sys\n\nprint(sys.argv)\n"
    after = "import sys\n\nprint(sys.argv)\n"
    e = one_edit(before, after)
    assert before.count(e["old"]) == 1 and before.replace(e["old"], e["new"]) == after
    same = "x\nx\nx\n"
    e = one_edit(same, "x\nx\n")
    assert same.replace(e["old"], e["new"], 1) == "x\nx\n"


def test_tool_jail_has_no_folder_and_no_network(host):
    jail = compile_tool("ruff", "/usr/local/bin/ruff", ("check", "-"), host)
    argv = " ".join(jail.argv)
    assert "--unshare-all" in argv and '"block_net":true' in argv
    assert "/home/me" not in argv and "--ro-bind /usr/local/bin/ruff /tool/ruff" in argv


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="needs bubblewrap")
def test_ruff_runs_jailed(tmp_path):
    from pythonator.policy.host import probe
    from pythonator.work import lint

    h = probe(str(tmp_path / "data"))
    if not h.landlock_abi or not lint.ruff_bin():
        pytest.skip("needs Landlock and ruff")
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n')
    try:
        probs = asyncio.run(lint.check(h, str(tmp_path), "a.py", "import os\n"))
        fixed = asyncio.run(lint.fix(h, str(tmp_path), "a.py", "import os\nprint(1)\n", "F401"))
    except Refused as e:
        pytest.skip(f"jail unavailable here: {e}")
    assert [p["code"] for p in probs] == ["F401"] and probs[0]["fixable"]
    assert fixed == "print(1)\n"
