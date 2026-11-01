from __future__ import annotations

import time
from pathlib import Path

from pythonator.core.logs import STALE_PARTIAL_SECONDS, LineKind, LogStore


def texts(lines: list) -> list[str]:  # type: ignore[type-arg]
    return [ln.text for ln in lines]


def test_lines_are_assembled_across_chunks(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log")
    s = store.stream()
    s.write("hel")
    s.write("lo\nwor")
    s.write("ld\r\nnext\n")
    assert texts(store.drain()) == ["hello", "world", "next"]
    assert store.drain() == []


def test_carriage_return_keeps_last_frame(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log")
    store.stream().write("10%\r50%\r100%\n")
    assert texts(store.drain()) == ["100%"]


def test_stale_partial_line_is_flushed(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log")
    store.stream().write("Your name? ")
    assert store.drain() == []
    time.sleep(STALE_PARTIAL_SECONDS + 0.05)
    assert texts(store.drain()) == ["Your name? "]


def test_close_flushes_partial(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log")
    s = store.stream()
    s.write("no newline")
    s.close()
    assert texts(store.drain()) == ["no newline"]


def test_file_has_timestamps_and_no_ansi(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log")
    store.stream().write("\x1b[31mred\x1b[0m\n")
    store.system("started")
    store.close()
    content = (tmp_path / "a.log").read_text().splitlines()
    assert content[0].endswith("] red") and content[0].startswith("[20")
    assert content[1].endswith("] [pythonator] started")


def test_ring_buffer_and_dropped_note(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "a.log", max_lines=10)
    store.stream().write("".join(f"{i}\n" for i in range(25)))
    drained = store.drain()
    assert drained[0].kind is LineKind.SYSTEM and "15 lines skipped" in drained[0].text
    assert texts(drained[1:]) == [str(i) for i in range(15, 25)]
    assert texts(store.snapshot()) == [str(i) for i in range(15, 25)]
    assert store.total_lines == 25


def test_rotation_and_search(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "svc.log", max_bytes=2000, backups=2)
    s = store.stream()
    for i in range(300):
        s.write(f"line {i} {'needle' if i % 50 == 0 else ''}\n")
    files = store.files()
    assert [f.name for f in files] == ["svc.log.2", "svc.log.1", "svc.log"]
    matches, total = store.search("NEEDLE")
    assert total == len(matches) >= 1
    assert all("needle" in m for m in matches)
    # Oldest data beyond the retained backups is gone, newest is kept.
    assert "line 299" in "\n".join(store.iter_file_lines())


def test_relocate_and_delete(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "old.log")
    store.system("hello")
    store.relocate(tmp_path / "new.log")
    store.system("again")
    assert not (tmp_path / "old.log").exists()
    assert "again" in (tmp_path / "new.log").read_text()
    store.clear(delete_files=True)
    assert store.files() == [] and store.snapshot() == []
