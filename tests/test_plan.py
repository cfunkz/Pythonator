"""Plan sheet backend: safe steps only, exposure, proposals, metadata naming, and safefs writes for Keep changes."""

import os

import pytest

from pythonator.model.service import ServiceSpec
from pythonator.net.gate import literal_why
from pythonator.policy.risk import exposure, safe_part, widenings
from pythonator.safe import safefs


def spec(**kw):
    base = {"name": "api", "folder": "/home/u/services/api", "command": ["main.py"]}
    return ServiceSpec.model_validate({**base, **kw}, strict=False)


def test_safe_part_takes_only_lines_that_dont_widen():
    old = spec(network={"traffic": {"mode": "hosts", "hosts": ["a.example.com", "b.example.com"]}})
    new = spec(
        command=["main.py", "--workers", "2"],
        network={"traffic": {"mode": "hosts", "hosts": ["b.example.com", "c.example.com"]}, "ports": [{"port": 8001}]},
        limits={"memory_mb": 384},
    )
    merged = safe_part(old, new)
    assert merged.command == ("main.py", "--workers", "2")
    assert merged.limits.memory_mb == 384
    assert merged.network.traffic.hosts == ("b.example.com",)  # the removal applies, the new host waits
    assert merged.network.ports == ()
    assert not widenings(old, merged)
    assert widenings(merged, new) == ["new outbound host c.example.com", "opens port 8001 to this computer"]


def test_exposure_orders_modes_and_reach():
    assert exposure(spec()) < exposure(spec(network={"traffic": {"mode": "any"}})) < exposure(spec(mode="full"))
    assert exposure(spec(mode="full", network={"traffic": {"mode": "any"}, "ports": [{"port": 9000, "reach": "lan"}]})) <= 10


@pytest.mark.parametrize(
    ("host", "why"),
    [("169.254.169.254", "cloud metadata"), ("127.0.0.1", "this computer"), ("192.168.1.4", "your local network"),
     ("::ffff:169.254.169.254", "cloud metadata"), ("pypi.org", ""), ("1.1.1.1", "")],
)
def test_gate_names_literal_addresses(host, why):
    assert literal_why(host, allow_lan=False) == why


@pytest.mark.skipif(os.name == "nt", reason="POSIX link test")
def test_write_bytes_and_unlink_refuse_links(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "out").symlink_to(outside)
    with pytest.raises(safefs.UnsafePath):
        safefs.write_bytes(str(root), "out/report.csv", b"x", make_parents=True)
    assert not (outside / "report.csv").exists()
    safefs.write_bytes(str(root), "new/deep/file.bin", b"\x00" * 70000, make_parents=True)
    assert (root / "new/deep/file.bin").stat().st_size == 70000
    (outside / "secret").write_text("keep me")
    (root / "secret").symlink_to(outside / "secret")
    with pytest.raises(safefs.UnsafePath):
        safefs.unlink(str(root), "secret")
    assert (outside / "secret").exists()
    safefs.unlink(str(root), "new/deep/file.bin")
    safefs.unlink(str(root), "new/deep/file.bin")  # already gone is fine


def test_try_copy_leaves_out_caches_links_and_big_folders(tmp_path, monkeypatch):
    from pythonator.reconcile import trycopy

    src = tmp_path / "svc"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref")
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "main.py").write_text("print(1)")
    (src / "data").mkdir()
    (src / "data" / "a.txt").write_text("a")
    if os.name != "nt":
        (src / "link").symlink_to(tmp_path)
    t = trycopy.TryCopies(tmp_path / "pdata", None, None)  # type: ignore[arg-type]
    c = t.make(spec(name="svc", folder=str(src)))
    assert c.path.parent.parent == tmp_path / "pdata-try"
    assert sorted(c.before) == ["data/a.txt", "main.py"]
    assert t.discard(c.id) and not c.path.exists()
    monkeypatch.setattr(trycopy, "MAX_BYTES", 3)
    with pytest.raises(trycopy.TooBig):
        t.make(spec(name="svc", folder=str(src)))
