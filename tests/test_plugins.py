import pytest

from pythonator.plugins.model import BadPlugin, content_hash, load_manifest


def write(tmp_path, toml, files=None):
    (tmp_path / "plugin.toml").write_text(toml)
    for k, v in (files or {"main.py": "x = 1\n"}).items():
        (tmp_path / k).write_text(v)
    return tmp_path


def test_manifest(tmp_path):
    m = load_manifest(write(tmp_path, '[plugin]\nid = "lat"\nname = "Latency"\npermissions = ["services.read"]\n'))
    assert (m.id, m.entry, m.permissions) == ("lat", "main.py", ("services.read",))


@pytest.mark.parametrize(
    "toml",
    [
        '[plugin]\nid = "Bad Id"\n',
        '[plugin]\nid = "ok"\nentry = "../x.py"\n',
        '[plugin]\nid = "ok"\npermissions = ["root.everything"]\n',
    ],
)
def test_bad_manifest(tmp_path, toml):
    with pytest.raises(BadPlugin):
        load_manifest(write(tmp_path, toml))


def test_hash_changes_with_code_and_refuses_links(tmp_path):

    d = write(tmp_path, '[plugin]\nid = "a"\n')
    h1 = content_hash(d)
    (d / "main.py").write_text("x = 2\n")
    assert content_hash(d) != h1
    from conftest import symlink_or_skip

    symlink_or_skip(d / "main.py", d / "link")
    with pytest.raises(BadPlugin):
        content_hash(d)


def test_bundled_guard_is_valid():
    from pythonator.plugins.host import BUNDLED

    m = load_manifest(BUNDLED / "guard")
    assert m.id == "guard" and "policy.check" in m.permissions


def _host(tmp_path, host):
    import dataclasses

    from pythonator.plugins.host import PluginHost

    class Api:
        def emit(self, *_a):
            pass

    return PluginHost(tmp_path / "data", dataclasses.replace(host, home=str(tmp_path)), Api())


def test_net_connect_grant_is_limited_to_declared_hosts(tmp_path, host):
    from pythonator.policy.plan import Refused

    (tmp_path / "src").mkdir()
    src = write(tmp_path / "src", '[plugin]\nid = "rep"\npermissions = ["net.connect", "ui.notify"]\n'
                'hosts = ["a.example.com", "b.example.com"]\n')
    h = _host(tmp_path, host)
    info = h.inspect(str(src))
    assert info["hosts"] == ["a.example.com", "b.example.com"] and info["files"] == 2
    h.install(str(src))
    h.grant("rep", "net.connect", ["a.example.com"])
    assert h.plugins["rep"].grants["net.connect"] == ["a.example.com"]
    with pytest.raises(Refused, match="didn't declare"):
        h.grant("rep", "net.connect", ["evil.example.com"])
    h.grant("rep", "net.connect", True)  # all declared hosts, never more
    assert h.plugins["rep"].grants["net.connect"] == ["a.example.com", "b.example.com"]


def test_new_code_resets_grants_and_marks_new_permissions(tmp_path, host):
    (tmp_path / "src").mkdir()
    src = write(tmp_path / "src", '[plugin]\nid = "lat"\nversion = "0.1.0"\npermissions = ["services.read"]\n')
    h = _host(tmp_path, host)
    h.install(str(src))
    h.grant("lat", "services.read")
    write(src, '[plugin]\nid = "lat"\nversion = "0.2.0"\npermissions = ["services.read", "ui.notify"]\n')
    info = h.install(str(src))
    p = h.plugins["lat"]
    assert p.grants == {} and p.code_changed and p.new_perms == ("ui.notify",)
    assert [x["new"] for x in info["permissions"]] == [False, True]
    h.grant("lat", "services.read")
    assert p.code_changed  # the new permission still waits for you
    h.reviewed("lat")
    assert not p.code_changed and p.new_perms == ()
    h.load()  # survives a restart of the background service
    assert h.plugins["lat"].grants == {"services.read": True} and not h.plugins["lat"].code_changed


def test_bundled_plugins_are_valid():
    from pythonator.plugins.host import BUNDLED

    ids = sorted(load_manifest(d).id for d in BUNDLED.iterdir() if (d / "plugin.toml").is_file())
    assert ids == ["ai", "builder", "guard", "notify"]
    ai = load_manifest(BUNDLED / "ai")
    assert ai.hosts == ("api.anthropic.com",) and ai.secrets == ("api_key",)
