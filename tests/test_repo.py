"""Rules for the repository itself: no program started by a bare name, lock files that are current and refreshed,
workflows on current actions, no committed .env files, and builds that fetch nothing unhashed."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


_SPAWN = {"run", "Popen", "call", "check_call", "check_output"}


def _bare(node: ast.expr) -> str | None:
    """The program name when ``node`` is a string constant with no folder in it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and not re.search(r"[\\/]", node.value):
        return node.value
    return None


def test_no_program_is_started_by_a_bare_name() -> None:
    """Windows looks for a bare program name in the current folder first, which can be a project a sandboxed service
    writes. Every program Pythonator starts is named by its full path (or found with which(), which skips it)."""
    found: list[str] = []
    for path in sorted((ROOT / "pythonator").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                name = func.id if isinstance(func, ast.Name) and func.id == "Popen" else \
                    func.attr if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id == "subprocess" else ""
                first = node.args[0]
                if name in _SPAWN and isinstance(first, ast.List) and first.elts and (bare := _bare(first.elts[0])):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}([{bare!r}, ...])")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "lpFile" and (bare := _bare(node.value)):
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno} lpFile = {bare!r}")
    assert not found, found


def _locks() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.lock") if ".git" not in p.parts and "node_modules" not in p.parts
                  and p.name.startswith("requirements"))


def test_every_lock_says_how_it_was_made_and_the_refresh_workflow_knows_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "refresh-locks.yml").read_text(encoding="utf-8")
    locks = _locks()
    assert len(locks) >= 4
    for lock in locks:
        rel = lock.relative_to(ROOT).as_posix()
        header = lock.read_text(encoding="utf-8").splitlines()[1].removeprefix("#    ")
        assert header.startswith("uv pip compile ") and header.endswith(f" -o {rel}"), header
        assert "--generate-hashes" in header and "--universal" in header
        assert rel in workflow  # refreshed every week
        assert rel in (ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")


def test_the_locked_versions_are_inside_the_declared_ranges() -> None:
    from packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", (ROOT / "requirements.lock").read_text(encoding="utf-8"),
                             re.MULTILINE))
    locked = {k.lower().replace("_", "-"): v for k, v in locked.items()}
    for line in project["project"]["dependencies"]:
        req = Requirement(line)
        assert req.specifier.contains(locked[req.name.lower()]), (line, locked[req.name.lower()])
    psutil = next(Requirement(x) for x in project["project"]["dependencies"] if x.startswith("psutil"))
    assert not psutil.specifier.contains("8.0.0")  # 8 changes Process.info and memory_info


def test_workflows_name_their_ubuntu_and_use_current_actions() -> None:
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "ubuntu-latest" not in text, path.name
        assert "attest-build-provenance@" not in text, path.name
        for sha, tag in re.findall(r"uses: actions/download-artifact@([0-9a-f]{40})\s+#\s*(\S+)", text):
            assert tag.startswith("v8"), (path.name, sha, tag)


def test_env_files_are_never_committed() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "!.env.example"} <= set(lines)


def test_builds_dont_fetch_setuptools_unhashed() -> None:
    for name in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "requirements-build.lock" in line and "pip install --no-deps" in line:
                assert "--no-build-isolation" in line, line
