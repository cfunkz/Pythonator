import stat
import sys

from pythonator.ledger.ledger import Ledger
from pythonator.logs.store import STRIDE, LogStore
from pythonator.model.service import ServiceSpec
from pythonator.safe.mask import Masker
from pythonator.store.specs import SpecStore


def test_logs_range_read(tmp_path):
    lg = LogStore(tmp_path, "svc")
    for i in range(STRIDE * 5 + 3):
        lg.append("o", f"line {i}")
    assert lg.total == STRIDE * 5 + 3
    got = lg.read(STRIDE * 2 + 1, 4)
    assert [x.text for x in got] == [f"line {i}" for i in range(STRIDE * 2 + 1, STRIDE * 2 + 5)]
    assert [x.text for x in lg.tail(2)] == [f"line {STRIDE * 5 + 1}", f"line {STRIDE * 5 + 2}"]
    again = LogStore(tmp_path, "svc")  # reopen: index is rebuilt from disk
    assert again.read(7, 1)[0].text == "line 7"


def test_logs_masked_on_disk(tmp_path):
    lg = LogStore(tmp_path, "svc", Masker(["topsecret"]))
    lg.append("o", "token topsecret")
    lg.close()
    raw = b"".join(p.read_bytes() for p in (tmp_path / "svc").glob("seg-*.log"))
    assert b"topsecret" not in raw


def test_ledger_survives_torn_line(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    e = led.intend("process", "svc", "pending")
    led.done(e, "123:1.0")
    with open(tmp_path / "ledger.jsonl", "ab") as f:
        f.write(b'{"id": 9, "op": "int')  # crash mid-write
    again = Ledger(tmp_path / "ledger.jsonl")
    assert [x.ref for x in again.live()] == ["123:1.0"]


def test_spec_store_generations_and_approval(tmp_path):
    st = SpecStore(tmp_path)
    s = ServiceSpec.model_validate({"name": "a", "folder": "/srv/a", "command": ["a.py"]}, strict=False)
    s1 = st.save(s)
    s2 = st.save(s1.evolve(command=["b.py"]))
    assert (s1.generation, s2.generation) == (1, 2)
    from pythonator.policy.risk import risk_hash

    assert not st.approve("a", "0" * 64)
    assert st.approve("a", risk_hash(s2))
    assert st.is_approved(st.get("a"))
    if sys.platform != "win32":  # no POSIX modes there; the data folder is in the user's private profile
        assert stat.S_IMODE((tmp_path / "services" / "a.json").stat().st_mode) == 0o600
