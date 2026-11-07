"""AI edit blocks, read the way models really write them: an edit's markers never end up in a file, a change that
can only be refused isn't offered, and a file gets one Review button."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
