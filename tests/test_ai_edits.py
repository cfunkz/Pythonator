"""AI edit blocks, read the way models really write them: an edit's markers never end up in a file, a change that
can only be refused isn't offered, and a file gets one Review button."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pythonator.plugins.files import FileRefused, ServiceFiles, check_added

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "pythonator-ai"
sys.path.insert(0, str(PLUGIN))

from pythonator_ai import context  # noqa: E402

from tests.test_ai_plugin import _assistant  # noqa: E402

APP = """import { useState } from 'react'
import './App.css'

function App() {
  const [count, setCount] = useState(0)

  return (
    <>
      <h1>Vite + React</h1>
      <button onClick={() => setCount((count) => count + 1)}>
        count is {count}
      </button>
    </>
  )
}

export default App
"""


def _no_markers(edits: dict[str, list[context.Edit]]) -> None:
    for items in edits.values():
        for e in items:
            assert not context.MARKER.search(e.new), e.new


def test_a_missing_end_never_puts_the_next_block_into_the_file() -> None:
    """What broke App.jsx: the first block had no >>>>>>> END, so its new text ran on through the next block, and
    that block's markers were written into the file."""
    answer = ("Two changes.\n\n<<<<<<< EDIT src/App.jsx\nimport './App.css'\n=======\nimport './App.css'\n"
              "import About from './About'\n\n<<<<<<< EDIT src/App.jsx\n      <h1>Vite + React</h1>\n=======\n"
              "      <h1>Hello</h1>\n>>>>>>> END\n")
    edits, notes = context.parse_answer(answer, {"src/App.jsx": APP})
    assert notes == []
    _no_markers(edits)
    assert [e.new for e in edits["src/App.jsx"]] == ["import './App.css'\nimport About from './About'\n\n",
                                                    "      <h1>Hello</h1>\n"]


@pytest.mark.parametrize("answer", [
    "<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> REPLACE\n",
    "```jsx\n<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> END\n```\n",
    "```jsx\n<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n```\nThat's it.\n",
    "  <<<<<<< EDIT `src/App.jsx`\n<h1>Vite + React</h1>\n  =======\n<h1>Hi</h1>\n  >>>>>>> END\n",
    "src/App.jsx\n```jsx\n<<<<<<< SEARCH\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> REPLACE\n```\n",
    "<<<<<<< EDIT src/App.jsx\r\n<h1>Vite + React</h1>\r\n=======\r\n<h1>Hi</h1>\r\n>>>>>>> END\r\n",
])
def test_the_formats_models_drift_into_are_read(answer: str) -> None:
    edits, notes = context.parse_answer(answer, {"src/App.jsx": APP})
    assert notes == [] and list(edits) == ["src/App.jsx"]
    edit = edits["src/App.jsx"][0]
    assert edit.old == "<h1>Vite + React</h1>\n" and edit.new == "<h1>Hi</h1>\n"


def test_a_block_without_a_separator_is_reported_not_guessed() -> None:
    edits, notes = context.parse_answer("<<<<<<< EDIT app.py\nPORT = 80\n>>>>>>> END\n")
    assert edits == {} and "isn't a whole edit block" in notes[0]


def test_new_text_holding_a_marker_is_never_accepted() -> None:
    answer = "<<<<<<< EDIT a.py\nx = 1\n=======\n=======\nx = 2\n>>>>>>> END\n"
    edits, _ = context.parse_answer(answer, {"a.py": "x = 1\n"})
    _no_markers(edits)


BROKEN = APP.replace("import { useState }", "<<<<<<< EDIT src/App.jsx\nimport { useState }", 1).replace(
    "export default App", "=======\nexport default App", 1)


def test_a_file_already_holding_markers_can_be_fixed() -> None:
    """The way out of a broken file: the model quotes the stray markers as the text to replace."""
    answer = ("Remove the leftover markers.\n\n<<<<<<< EDIT src/App.jsx\n<<<<<<< EDIT src/App.jsx\n"
              "import { useState } from 'react'\n=======\nimport { useState } from 'react'\n>>>>>>> END\n\n"
              "<<<<<<< EDIT src/App.jsx\n=======\nexport default App\n=======\nexport default App\n>>>>>>> END\n")
    edits, notes = context.parse_answer(answer, {"src/App.jsx": BROKEN})
    assert notes == []
    fixed, why = context.resolve_paths(edits, ["src/App.jsx"], {"src/App.jsx": BROKEN})
    assert why == []
    text = BROKEN
    for e in fixed["src/App.jsx"]:
        assert text.count(e.old) == 1
        text = text.replace(e.old, e.new, 1)
    assert text == APP


def test_indentation_differences_are_matched_and_the_new_text_follows_the_file() -> None:
    answer = ("<<<<<<< EDIT src/App.jsx\n<button onClick={() => setCount((count) => count + 1)}>\n  count is {count}\n"
              "</button>\n=======\n<button onClick={() => setCount(count + 1)}>\n  clicked {count}\n</button>\n"
              ">>>>>>> END\n")
    edits, _ = context.resolve_paths(context.parse_edits(answer), ["src/App.jsx"], {"src/App.jsx": APP})
    edit = edits["src/App.jsx"][0]
    assert APP.count(edit.old) == 1
    assert edit.new == ("      <button onClick={() => setCount(count + 1)}>\n        clicked {count}\n"
                        "      </button>\n")


def test_a_change_that_can_only_be_refused_is_not_offered_and_says_why() -> None:
    answer = ("<<<<<<< EDIT src/App.jsx\nimport viteLogo from '/vite.svg'\n=======\nimport x from 'y'\n>>>>>>> END\n"
              "<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> END\n")
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["src/App.jsx"], {"src/App.jsx": APP})
    assert edits == {}
    assert "src/App.jsx isn't offered: edit 1 of 2: the text it replaces isn't in the file" in notes[0]
    # In order, as Pythonator applies them: the second can't replace what the first already changed.
    answer = ("<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> END\n" * 2)
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["src/App.jsx"], {"src/App.jsx": APP})
    assert len(edits["src/App.jsx"]) == 1 and notes == []  # the same block twice is one edit
    answer = ("<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Hi</h1>\n>>>>>>> END\n"
              "<<<<<<< EDIT src/App.jsx\n<h1>Vite + React</h1>\n=======\n<h1>Bye</h1>\n>>>>>>> END\n")
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["src/App.jsx"], {"src/App.jsx": APP})
    assert edits == {} and "edit 2 of 2" in notes[0]


def test_a_whole_new_version_of_an_existing_file_replaces_it() -> None:
    new = APP.replace("Vite + React", "Hello")
    edits, notes = context.resolve_paths(context.parse_edits(f"<<<<<<< EDIT src/App.jsx\n=======\n{new}>>>>>>> END\n"),
                                         ["src/App.jsx"], {"src/App.jsx": APP})
    assert notes == [] and edits["src/App.jsx"][0].old == APP and edits["src/App.jsx"][0].new == new


def test_a_stand_in_for_a_secret_is_never_written() -> None:
    text = 'TOKEN = "[TOKEN hidden]"\nDEBUG = False\n'
    answer = "<<<<<<< EDIT bot.py\nDEBUG = False\n=======\nDEBUG = True\nKEY = \"[secret removed]\"\n>>>>>>> END\n"
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["bot.py"], {"bot.py": text})
    assert edits == {} and "stand-in for a hidden value" in notes[0]


@pytest.mark.parametrize("line", ["<<<<<<< EDIT src/App.jsx", "=======", ">>>>>>> END", ">>>>>>> REPLACE",
                                  'KEY = "[secret removed]"', "token = '[BOT_TOKEN hidden]'"])
def test_pythonator_itself_refuses_writing_markers_or_stand_ins(line: str) -> None:
    with pytest.raises(FileRefused, match="malformed"):
        check_added(["a = 1"], ["a = 1", line])


def test_pythonator_refuses_a_proposal_that_would_break_the_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text(APP, encoding="utf-8")
    files = ServiceFiles(tmp_path)
    with pytest.raises(FileRefused, match="edit marker"):
        files.propose("p", "s", "src/App.jsx", [{"old": "import './App.css'\n",
                                                 "new": "<<<<<<< EDIT src/App.jsx\nimport './App.css'\n"}], "")


def test_one_review_button_per_file_from_the_latest_answer(tmp_path: Path) -> None:
    a = _assistant(tmp_path)
    a.selected = "s1"
    edit = [context.Edit("src/App.jsx", "a\n", "b\n")]
    turns = a.chats.setdefault("s1", [])
    for _ in range(2):
        turn = sys.modules["ai_plugin_under_test"].Turn("fix it", status="done")
        turn.edits = {"src/App.jsx": list(edit)}
        turns.append(turn)
    a.render(force=True)
    buttons = [i for row in a.panels[-1]["items"] if row["type"] == "row" for i in row["items"]
               if i.get("action") == "propose"]
    assert [b["value"] for b in buttons] == ["1:src/App.jsx"]
    turns[1].proposals["src/App.jsx"] = "Refused: something"  # refused: asking again is the way, not a retry
    a.render(force=True)
    assert "Review change" not in json.dumps(a.panels[-1])


SCRIPT = """import math

def calculate():
    number = 16
    print(math.sqrt(number))

if __name__ == "__main__":
    calculate()
"""


@pytest.mark.parametrize("before", ["", "\n", "print('hi')\n", "print('hi')\r\nx = 1\r\n"])
def test_a_whole_file_answer_is_applied_to_an_existing_file(tmp_path: Path, before: str) -> None:
    """0.0.17: a model asked to write main.py writes it whole, with nothing before =======. For a main.py that is
    there, even empty, Pythonator refused it ("main.py exists, so say which text to replace")."""
    (tmp_path / "main.py").write_bytes(before.encode())
    shown = {"main.py": before}
    answer = f"Here is the script.\n\n<<<<<<< EDIT main.py\n=======\n{SCRIPT}>>>>>>> END\n"
    edits, notes = context.resolve_paths(context.parse_edits(answer, shown), ["main.py"], shown)
    assert notes == []
    proposal = ServiceFiles(tmp_path).propose("p", "s", "main.py", [{"old": e.old, "new": e.new}
                                                                     for e in edits["main.py"]], "")
    assert proposal.after.replace("\r\n", "\n") == SCRIPT


def test_a_whole_file_answer_for_a_file_the_assistant_never_saw_is_not_offered() -> None:
    answer = f"<<<<<<< EDIT main.py\n=======\n{SCRIPT}>>>>>>> END\n"
    edits, notes = context.resolve_paths(context.parse_edits(answer), ["main.py"], {})
    assert edits == {} and "didn't see main.py" in notes[0]


def test_pythonator_still_wants_the_text_to_replace_in_a_file_with_content(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    with pytest.raises(FileRefused, match="isn't empty"):
        ServiceFiles(tmp_path).propose("p", "s", "main.py", [{"old": "", "new": SCRIPT}], "")


# -- several files, one review (0.0.18) -------------------------------------------------------------------------


def _project(tmp_path: Path) -> ServiceFiles:
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "index.html").write_text("<p>old</p>\n", encoding="utf-8")
    return ServiceFiles(tmp_path)


def test_one_change_creates_files_in_new_folders_and_changes_another(tmp_path: Path) -> None:
    files = _project(tmp_path)
    proposal = files.propose_many("p", "s", [
        {"path": "main.py", "edits": [{"old": "print('hi')\n", "new": "print('game')\n"}]},
        {"path": "static/index.html", "edits": [{"old": "", "new": "<h1>Game</h1>\n"}]},
        {"path": "static/js/game.js", "edits": [{"old": "", "new": "console.log(1)\n"}]}], "")
    assert proposal.paths == ["main.py", "static/index.html", "static/js/game.js"]
    assert not (tmp_path / "static").exists()
    backups = files.apply_all(proposal, tmp_path.parent / "backups")
    assert (tmp_path / "static" / "js" / "game.js").read_text() == "console.log(1)\n"
    assert (tmp_path / "main.py").read_text() == "print('game')\n"
    assert backups[0] and backups[1:] == ["", ""]  # a copy of the old main.py; the others are new


@pytest.mark.parametrize(("path", "why"), [
    (".env", "secrets"), ("config/.env.local", "secrets"), ("../outside.py", "inside the service's folder"),
    ("deploy.sh", "script or program"), ("main.py", "twice"), ("templates/index.html", "isn't empty")])
def test_one_bad_file_refuses_the_whole_change(tmp_path: Path, path: str, why: str) -> None:
    files = _project(tmp_path)
    with pytest.raises(FileRefused, match=why):
        files.propose_many("p", "s", [
            {"path": "main.py", "edits": [{"old": "print('hi')\n", "new": "print('game')\n"}]},
            {"path": path, "edits": [{"old": "", "new": "X = 1\n"}]}], "")


def test_a_new_folder_through_a_link_is_refused(tmp_path: Path) -> None:
    files = _project(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    try:
        (tmp_path / "static").symlink_to(outside, target_is_directory=True)
    except OSError:  # Windows without the right to make links
        pytest.skip("can't make a link here")
    with pytest.raises(FileRefused):
        files.propose_many("p", "s", [{"path": "static/index.html", "edits": [{"old": "", "new": "x\n"}]}], "")
    assert not list(outside.iterdir())


def test_all_files_or_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _project(tmp_path)
    proposal = files.propose_many("p", "s", [
        {"path": "main.py", "edits": [{"old": "print('hi')\n", "new": "print('game')\n"}]},
        {"path": "static/index.html", "edits": [{"old": "", "new": "<h1>Game</h1>\n"}]},
        {"path": "notes.md", "edits": [{"old": "", "new": "# Notes\n"}]}], "")
    (tmp_path / "notes.md").write_text("mine\n", encoding="utf-8")  # made meanwhile: nothing is written
    with pytest.raises(FileRefused, match=r"notes\.md changed"):
        files.apply_all(proposal, tmp_path.parent / "backups")
    assert (tmp_path / "main.py").read_text() == "print('hi')\n" and not (tmp_path / "static").exists()
    (tmp_path / "notes.md").unlink()
    real = ServiceFiles.apply

    def fail_third(self: ServiceFiles, part: Any, backups: Path) -> str:
        if part.path == "notes.md":
            raise OSError("disk full")
        return real(self, part, backups)

    monkeypatch.setattr(ServiceFiles, "apply", fail_third)
    with pytest.raises(OSError):
        files.apply_all(proposal, tmp_path.parent / "backups")
    assert (tmp_path / "main.py").read_text() == "print('hi')\n", "put back"
    assert not (tmp_path / "static").exists(), "taken away again, with the folder it made"


def test_the_assistant_offers_several_files_as_one_review(tmp_path: Path) -> None:
    a = _assistant(tmp_path, model="m")
    a.selected = "s1"
    a.permissions = frozenset({"ui", "propose_changes"})
    sent: list[Any] = []
    def propose(sid: str, changes: Any, summary: str) -> str:
        sent.append(changes)
        return "abc"

    a.propose_changes = propose
    turn = sys.modules["ai_plugin_under_test"].Turn("make a game", status="done")
    turn.edits = {"main.py": [context.Edit("main.py", "a\n", "b\n")],
                  "static/index.html": [context.Edit("static/index.html", "", "<h1>x</h1>\n")]}
    a.chats["s1"] = [turn]
    a.render(force=True)
    buttons = [i for row in a.panels[-1]["items"] if row["type"] == "row" for i in row["items"]
               if i.get("action") == "propose"]
    assert [(b["label"], b["value"]) for b in buttons] == [
        ("Review changes to 2 files (main.py, static/index.html)", "0:*")]
    a._propose("s1", 0, "*")
    assert sent == [[("main.py", [("a\n", "b\n")]), ("static/index.html", [("", "<h1>x</h1>\n")])]]
    a.on_proposal("abc", "applied", "Saved 2 files")
    a.render(force=True)
    text = json.dumps(a.panels[-1])
    assert "main.py, static/index.html: Applied: Saved 2 files" in text and "Review change" not in text


def test_a_new_file_in_a_named_folder_stays_there() -> None:
    """static/index.html is a new file, not the one index.html the service already has."""
    answer = "<<<<<<< EDIT static/index.html\n=======\n<h1>Game</h1>\n>>>>>>> END\n"
    shown = {"templates/index.html": "<p>old</p>\n"}
    edits, notes = context.resolve_paths(context.parse_edits(answer, shown), ["templates/index.html"], shown)
    assert list(edits) == ["static/index.html"] and notes == []


@pytest.mark.parametrize("path", ["script.js", "some_module.py", "static/script.js", "your_bot.py"])
def test_a_new_file_with_a_common_name_is_created(path: str) -> None:
    edits, notes = context.resolve_paths(context.parse_edits(f"<<<<<<< EDIT {path}\n=======\nx = 1\n>>>>>>> END\n"),
                                         ["main.py"], {"main.py": "print()\n"})
    assert list(edits) == [path] and notes == []


@pytest.mark.parametrize("path", ["path/to/file.py", "<file>.py", "file.py"])
def test_a_placeholder_new_file_is_still_left_out(path: str) -> None:
    edits, _ = context.resolve_paths(context.parse_edits(f"<<<<<<< EDIT {path}\n=======\nx = 1\n>>>>>>> END\n"),
                                     ["main.py"], {"main.py": "print()\n"})
    assert edits == {}



@pytest.mark.parametrize("path", [
    "run.bat.", "run.bat ", "notes.txt:hidden", "run.bat::$DATA", "con.txt", "static/NUL", "lpt1.js",
    "dockerfile", "DOCKERFILE", "Makefile", "pyvenv.cfg", ".npmrc", "lib/extra.pth", "sitecustomize.py",
    ".github/workflows/ci.yml", ".GitHub/workflows/ci.yml", ".git/config", ".git", "node_modules/x/index.js",
    ".venv/lib/x.py", "compose.yaml", ".ssh/authorized_keys", "keys/server.pem", "deploy.SH", "start.ps1",
])
def test_the_assistant_can_never_write_these(tmp_path: Path, path: str) -> None:
    """Names a tool would run, or that Windows writes as another file, and the folders plugins never see."""
    with pytest.raises(FileRefused):
        ServiceFiles(tmp_path).propose_many("p", "s", [{"path": path, "edits": [{"old": "", "new": "x = 1\n"}]}], "")
    assert list(tmp_path.iterdir()) == []


def test_at_most_twelve_files_and_a_megabyte_in_one_change(tmp_path: Path) -> None:
    files = ServiceFiles(tmp_path)
    many = [{"path": f"f{i}.txt", "edits": [{"old": "", "new": "x\n"}]} for i in range(13)]
    with pytest.raises(FileRefused, match="1 to 12 files"):
        files.propose_many("p", "s", many, "")
    big = [{"path": f"f{i}.txt", "edits": [{"old": "", "new": "x" * 40_000 + "\n"}]} for i in range(12)]
    assert files.propose_many("p", "s", big, "").paths[-1] == "f11.txt"  # 480 KB: fine
