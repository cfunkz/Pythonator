"""0.0.15: a runtime too old for a project or framework is named before anything runs (for every runtime), the
plugin dock stays smooth while it is resized or the AI streams, many more project templates, a start that never
listens says so, a port this computer refuses is named at start, and the sandbox's Rust and dev tools work."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator.core import envcheck, runtimes, templates, versions
from pythonator.core.models import Service

# -- versions --------------------------------------------------------------------------------------------------


def test_versions_are_read_from_what_each_program_prints() -> None:
    assert versions.parse("v22.13.0") == (22, 13, 0)
    assert versions.parse("Python 3.13.1") == (3, 13, 1)
    assert versions.parse("go version go1.24.7 linux/amd64") == (1, 24, 7)
    assert versions.parse('openjdk version "21" 2023-09-19') == (21, 0, 0)
    assert versions.parse("no digits") is None


@pytest.mark.parametrize(("version", "spec", "ok"), [
    ((22, 13, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", False),  # Nuxt 4 on the Node.js of the user's log
    ((22, 22, 3), "^22.22.3 || ^24.15.0 || >=26.0.0", True),
    ((23, 0, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", False),
    ((26, 1, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", True),
    ((20, 19, 0), "^20.19.0 || >=22.12.0", True),
    ((21, 0, 0), "^20.19.0 || >=22.12.0", False),
    ((3, 9, 0), ">=3.10", False),
    ((3, 12, 1), ">= 3.10, <4", True),
    ((3, 12, 1), "~=3.10", True),
    ((4, 0, 0), "~=3.10", False),
    ((8, 3, 4), "^8.2|^8.3", True),
    ((22, 5, 0), "22.x", True),
    ((23, 0, 0), "22", False),
    ((1, 2, 3), "latest", True),  # nothing to read: never blocks
])
def test_npm_style_ranges(version: versions.Version, spec: str, ok: bool) -> None:
    assert versions.satisfies(version, spec) is ok


def test_a_shortfall_says_what_to_install() -> None:
    assert versions.problem("node", (22, 13, 0), "^22.22.3 || ^24.15.0 || >=26.0.0", what="Nuxt") == (
        "Nuxt needs Node.js 22.22.3 or newer (this computer has 22.13.0): install the current LTS from nodejs.org")
    assert versions.problem("node", (22, 22, 3), ">=20") == ""
    assert versions.problem("node", None, ">=99") == ""  # unknown: never blocks
    assert versions.shortfall("python", (3, 9, 0), ">=3.10")[0] == "Python 3.10.0 or newer (this computer has 3.9.0)"


def test_each_runtimes_project_files_say_which_versions_it_needs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": ">=22.12", "bun": ">=1.2"}}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.18\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nrust-version = "1.85"\n')
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"php": "^8.2"}}))
    assert versions.declared("node", tmp_path) == (">=22.12", "package.json (engines)")
    assert versions.declared("bun", tmp_path)[0] == ">=1.2"
    assert versions.declared("python", tmp_path) == (">=3.11", "pyproject.toml (requires-python)")
    assert versions.declared("go", tmp_path)[0] == ">=1.18"
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.25.0\n")
    assert versions.declared("go", tmp_path)[0] == ">=1.21"  # Go 1.21+ fetches the toolchain a go.mod asks for
    assert versions.declared("rust", tmp_path)[0] == ">=1.85"
    assert versions.declared("php", tmp_path)[0] == "^8.2"
    assert versions.declared("ruby", tmp_path) == ("", "")
    (tmp_path / "package.json").write_text("{broken")
    assert versions.declared("node", tmp_path) == ("", "")


def test_a_project_that_needs_a_newer_runtime_is_named_before_it_runs(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": "^22.22.3 || >=24.15.0"}}))
    (tmp_path / "index.js").write_text("")
    monkeypatch.setattr(runtimes, "program", lambda rt, folder: "/bin/node")
    monkeypatch.setattr(versions, "installed", lambda prog: (22, 13, 0))
    service = Service.model_validate({"name": "n", "folder": str(tmp_path), "runtime": "node", "entry": "index.js"})
    errors = [text for level, text in envcheck.service_report(service) if level == "error"]
    assert errors == ["This project (package.json (engines)) needs Node.js 22.22.3 or newer (this computer has "
                      "22.13.0): install the current LTS from nodejs.org"]
    monkeypatch.setattr(versions, "installed", lambda prog: (24, 15, 0))
    assert not [t for level, t in envcheck.service_report(service) if level == "error"]


def test_the_version_of_a_real_program_is_asked_once(tmp_path: Path) -> None:
    import sys

    v = versions.installed(sys.executable)
    assert v is not None and v[:2] == sys.version_info[:2]
    assert versions.installed(str(tmp_path / "missing")) is None


# -- templates -------------------------------------------------------------------------------------------------


def test_a_big_list_of_templates_for_every_runtime() -> None:
    groups = {t.group for t in templates.TEMPLATES.values()}
    assert {"Node.js", "Python", "Go", "Rust", "Bun", "Deno", "Java", "PHP", "Ruby"} <= groups
    assert len(templates.TEMPLATES) >= 38
    for key in ("react", "vue", "svelte", "nuxt", "next", "astro", "angular", "express", "fastify", "hono", "koa",
                "fastapi", "flask", "django", "streamlit", "gradio", "go", "gin", "axum", "elysia", "deno", "php"):
        assert key in templates.TEMPLATES
    for t in templates.TEMPLATES.values():
        assert t.needs in templates.NEEDS and t.group, t.key
        if t.kind == "web":
            assert t.engines, f"{t.key} says which runtime versions it supports"


def test_the_catalog_says_what_is_missing_and_how_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}" if name in ("npm", "node") else None)
    monkeypatch.setattr(versions, "installed", lambda prog: (22, 13, 0))
    listed = {t["key"]: t for t in templates.catalog()}
    assert listed["express"]["ready"] and listed["react"]["ready"]
    assert not listed["nuxt"]["ready"] and listed["nuxt"]["needs"].startswith("Node.js 22.22.3 or newer")
    assert listed["nuxt"]["how"] == "install the current LTS from nodejs.org"
    assert not listed["go"]["ready"] and listed["go"]["needs"] == "Go" and listed["go"]["group"] == "Go"


def test_a_too_old_node_is_refused_with_what_to_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(versions, "installed", lambda prog: (22, 13, 0))
    with pytest.raises(ValueError, match=r"Nuxt needs Node.js 22.22.3 or newer \(this computer has 22.13.0\): "):
        templates.plan("nuxt", "app", tmp_path, set())
    assert templates.plan("react", "app", tmp_path, set()).steps  # Vite runs on 22.13


def test_templates_for_other_runtimes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    php = templates.plan("php", "site", tmp_path, set())
    port = php.service["network"]["ports"][0]
    assert php.service["runtime"] == "custom"
    assert php.service["command"] == ["/bin/php", "-S", f"127.0.0.1:{port}", "-t", "public"]
    gin = templates.plan("gin", "api", tmp_path, set())
    assert gin.steps == [["/bin/go", "mod", "tidy"]] and "module api" in gin.files["go.mod"]
    astro = templates.plan("astro", "site", tmp_path, set())
    package = json.loads(astro.files["package.json"])
    assert package["engines"]["node"] == ">=22.23.4" and package["scripts"]["dev"] == "astro dev"
    assert "--ignore-lock" in astro.service["args"]  # never a background server the service can't see
    assert not astro.steps  # its own files: nothing fetched from GitHub
    elysia = templates.plan("elysia", "e", tmp_path, set())
    assert json.loads(elysia.files["package.json"])["type"] == "module"


def test_the_builder_filters_by_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator import sdk
    from tests.test_official_plugins import plugin_module

    monkeypatch.setattr(sdk, "_send", lambda _m: None)
    builder = plugin_module("builder")
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}))
    shown: list[dict[str, Any]] = []
    monkeypatch.setattr(plugin, "project_templates", lambda: {"templates": [
        {"key": "vue", "label": "Vue", "tool": "create-vue", "port": 5173, "kind": "web", "group": "Node.js",
         "ready": True, "needs": "", "how": ""},
        {"key": "go", "label": "Go", "tool": "net/http", "port": 8080, "kind": "web", "group": "Go",
         "ready": False, "needs": "Go", "how": ""}], "folder": "/p"})
    monkeypatch.setattr(plugin, "list_projects", list)
    monkeypatch.setattr(plugin, "show", shown.append)
    plugin.on_start()
    deadline = time.monotonic() + 5
    while plugin.loading and time.monotonic() < deadline:
        time.sleep(0.01)
    assert '"Vue' in json.dumps(shown[-1]) and '"Go' in json.dumps(shown[-1])
    plugin.on_action("group", "Go")
    text = json.dumps(shown[-1])
    assert '"Go' in text and "Vue" not in text
    plugin.on_action("group", "nonsense")
    assert plugin.group == builder.ALL


# -- a start that doesn't listen, and ports this computer refuses ----------------------------------------------


def test_a_port_in_use_is_named_before_the_start(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        service = Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "python",
                                          "entry": "x.py", "network": {"ports": [port]}})
        problems = envcheck.port_problems(service)
        assert len(problems) == 1 and f"Port {port} is already in use" in problems[0]
        off = service.model_copy(update={"network": service.network.model_copy(update={"enabled": False})})
        assert envcheck.port_problems(off) == []
    assert envcheck.port_problems(service) == []  # free again


def test_a_reserved_windows_port_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(envcheck, "IS_WINDOWS", True)
    monkeypatch.setattr(envcheck, "_port_refused", lambda port: "reserved")
    service = Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "node", "entry": "x.js",
                                      "network": {"ports": [5173]}})
    (text,) = envcheck.port_problems(service)
    assert "Windows has reserved port 5173" in text and "excludedportrange" in text


def test_a_start_that_never_listens_says_where_it_is_stuck(harness: Any, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.daemon import supervisor

    monkeypatch.setattr(supervisor, "LISTEN_AFTER", 1)
    folder = tmp_path / "quiet"
    folder.mkdir()
    (folder / "quiet.py").write_text("import time\ntime.sleep(60)\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sid = harness.add(name="quiet", folder=str(folder), runtime="python", entry="quiet.py", mode="sandbox",
                      own_env=False, network={"ports": [port]})
    harness.ok({"op": "start", "id": sid})
    sup = harness.daemon.sup
    harness.wait(lambda: "Not listening on port" in sup.tail(sid), timeout=30_000)
    tail = sup.tail(sid)
    assert f"Not listening on port {port} after 1 s" in tail and "quiet.py" in tail
    harness.ok({"op": "stop", "id": sid})


# -- the sandbox: Rust through rustup, dev tools' own ports ----------------------------------------------------


def test_rustups_toolchains_are_found_for_a_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.isolation.base import rustup_home

    proxy = tmp_path / ".cargo" / "bin" / "cargo"
    (tmp_path / ".rustup" / "toolchains").mkdir(parents=True)
    monkeypatch.delenv("RUSTUP_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert rustup_home(proxy) == tmp_path / ".rustup"
    assert rustup_home(tmp_path / "usr" / "bin" / "cargo") is None  # a distribution's cargo: no rustup
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "elsewhere"))
    assert rustup_home(proxy) is None  # no toolchains there


def test_a_sandbox_with_ports_may_also_take_one_the_system_picks(tmp_path: Path) -> None:
    from pythonator import launcher

    if launcher.landlock_abi() < 4:
        pytest.skip("Landlock can't restrict ports on this kernel")
    import subprocess
    import sys

    code = ("import socket\nfrom pythonator import launcher\n"
            "launcher.apply_landlock(['/'], ['/tmp'], block_tcp=False, scope=False, bind_ports=PORTS)\n"
            "s = socket.socket()\ntry:\n    s.bind(('127.0.0.1', 0)); print('picked')\n"
            "except OSError:\n    print('refused')\n"
            "t = socket.socket()\ntry:\n    t.bind(('127.0.0.1', 1)); print('other')\n"
            "except OSError:\n    print('no other')\n")
    root = str(Path(__file__).resolve().parent.parent)

    def run(ports: list[int]) -> str:
        done = subprocess.run([sys.executable, "-c", code.replace("PORTS", repr(ports))], capture_output=True,
                              text=True, cwd=root, check=False, timeout=60)
        return " ".join(done.stdout.split())

    assert run([5999]) == "refused no other"
    assert run([5999, 0]) == "picked no other"


def test_linux_sandbox_plans_add_the_picked_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pythonator.isolation import linux
    from pythonator.isolation.base import Policy

    monkeypatch.setattr(linux, "support", lambda: linux.Support(landlock=4, seccomp="", bwrap=""))
    for ports, expected in (((5173,), [5173, 0]), ((), [])):
        cmd = linux.wrap(["node"], tmp_path, {}, Policy(network=True, ports=ports), tmp_path / "h", tmp_path / "t")
        plan = json.loads(cmd.argv[-1])
        assert plan["landlock"]["ports"] == expected


# -- the plugin dock -------------------------------------------------------------------------------------------


def _panel(n: int, text: str = "word ") -> dict[str, Any]:
    return {"title": f"P{n}", "items": [{"type": "text", "text": text * 40} for _ in range(10)]}


def test_redraws_are_coalesced_and_hidden_tabs_wait(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator.ui.panels import REDRAW_MS, PanelDock

    dock = PanelDock(lambda *a: None)
    qtbot.addWidget(dock)
    dock.show()
    drawn: list[str] = []
    dock.update_panel("a", "A", _panel(0))
    dock.update_panel("b", "B", _panel(0))
    original = PanelDock._draw

    def counted(self: PanelDock, page: Any, name: str, panel: dict[str, Any]) -> None:
        drawn.append(page.plugin)
        original(self, page, name, panel)

    monkeypatch.setattr(PanelDock, "_draw", counted)
    for i in range(1, 20):  # a streamed answer: twenty updates
        dock.update_panel("a", "A", _panel(i))
        dock.update_panel("b", "B", _panel(i))
    assert drawn == [] and dock.tabText(dock.indexOf(dock.pages["a"])) == "P19"
    qtbot.wait(REDRAW_MS * 3)
    assert drawn == ["a"] and dock.pages["a"].title == "P19"  # once, the latest; b is hidden
    assert "b" in dock.pending
    dock.setCurrentWidget(dock.pages["b"])
    assert drawn == ["a", "b"] and not dock.pending
    dock.update_panel("a", "A", None)
    assert "a" not in dock.pages


def test_a_resize_lays_the_panel_out_once_it_rests(qtbot: Any) -> None:
    from pythonator.ui.panels import REFLOW_MS, PanelPage

    page = PanelPage("p", lambda *a: None)
    qtbot.addWidget(page)
    page.resize(400, 300)
    page.show()
    page.show_panel(_panel(1))
    body, port = page.widget(), page.viewport()
    assert body is not None and port is not None
    qtbot.waitUntil(lambda: body.width() == port.width())
    first = body.width()
    for width in range(400, 600, 10):  # a drag
        page.resize(width, 300)
        qtbot.wait(5)
    assert body.width() == first  # not at every step
    qtbot.wait(REFLOW_MS * 3)
    assert body.width() == port.width() != first


def test_a_label_remembers_its_height_per_width(qtbot: Any) -> None:
    from pythonator.ui.panels import WrapLabel

    label = WrapLabel("word " * 200)
    qtbot.addWidget(label)
    h1 = label.heightForWidth(200)
    assert label.heightForWidth(200) == h1 and len(label._heights) == 1
    assert label.heightForWidth(400) < h1 and len(label._heights) == 2
    label.setText("short")
    assert label._heights == {} and label.heightForWidth(200) < h1
    for w in range(100, 140):
        label.heightForWidth(w)
    assert len(label._heights) <= 33
    assert label.textFormat().name == "PlainText"  # a plugin's text is never HTML
