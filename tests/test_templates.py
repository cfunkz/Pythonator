"""The Project builder and its templates: every template is valid code, starts from patched versions, asks for
runtimes that are supported, and stops cleanly."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from pythonator.core import runtimes, templates, versions
from pythonator.core import template_files as tf
from pythonator.core.models import Service


def test_templates_plan_new_folders_with_official_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    plan = templates.plan("react", "shop", tmp_path, set())
    assert plan.steps == [["/bin/npm", "create", "vite@9", "shop", "--", "--template", "react", "--no-interactive"]]
    assert plan.flatten == "shop" and plan.service["mode"] == "sandbox"
    assert plan.service["network"] == {"enabled": True, "ports": [plan.service["network"]["ports"][0]],
                                       "expose": "local"}
    django = templates.plan("django", "my-site", tmp_path, set())
    assert django.steps[-1][-4:] == ["django", "startproject", "my_site", "."]
    assert "Django" in django.files["requirements.txt"]
    flask = templates.plan("flask", "api", tmp_path, {5050})
    assert flask.service["network"]["ports"] != [5050]  # another service has that port
    compile(flask.files["app.py"], "app.py", "exec")
    compile(templates.plan("fastapi", "x", tmp_path, set()).files["main.py"], "main.py", "exec")
    for bad in ("Shop", "1x", "a/b", "../x", "x" * 41, ""):
        with pytest.raises(ValueError, match="lowercase"):
            templates.plan("react", bad, tmp_path, set())
    (tmp_path / "taken").mkdir()
    with pytest.raises(ValueError, match="exists already"):
        templates.plan("flask", "taken", tmp_path, set())
    with pytest.raises(ValueError, match="Unknown"):
        templates.plan("rails", "x", tmp_path, set())


def test_flatten_moves_the_tool_output_up(tmp_path: Path) -> None:
    (tmp_path / "shop" / "src").mkdir(parents=True)
    (tmp_path / "shop" / "package.json").write_text("{}")
    templates.flatten(tmp_path, "shop")
    assert (tmp_path / "package.json").is_file() and (tmp_path / "src").is_dir() and not (tmp_path / "shop").exists()


def test_builder_makes_a_flask_service_in_its_sandbox(dirs: Any, qtbot: Any, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The real thing: a new folder, the venv and pip run in the new service's sandbox (no packages, so it works
    offline)."""
    import dataclasses

    from tests.conftest import DaemonHarness

    offline = dataclasses.replace(templates.TEMPLATES["flask"], requirements="")
    monkeypatch.setitem(templates.TEMPLATES, "flask", offline)
    h = DaemonHarness(dirs, qtbot)
    try:
        sup = h.daemon.sup
        parent = tmp_path / "projects"
        service = sup.create_project("flask", "hello", parent)
        assert (parent / "hello" / "app.py").is_file() and service.mode.value == "sandbox"
        with pytest.raises(ValueError, match="exists already"):
            sup.create_project("flask", "hello", parent)
        with pytest.raises(ValueError, match="can't go in"):
            sup.create_project("flask", "x", dirs.root)
        h.wait(lambda: sup.slots[service.id].state.value in ("stopped", "failed"), timeout=240_000)
        log = sup.tail(service.id)
        assert sup.slots[service.id].state.value == "stopped", log
        assert "Project ready" in log and runtimes.project_venv(parent / "hello") is not None
    finally:
        h.close()


@pytest.mark.parametrize("key", [k for k, t in templates.TEMPLATES.items() if not t.command])
def test_every_written_template_is_valid_code(key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    # Not this computer's /bin/python3 or node: on Ubuntu 22.04 (Python 3.10) Streamlit's ">=3.11" refused it.
    monkeypatch.setattr(versions, "installed", lambda prog: None)
    plan = templates.plan(key, "demo", tmp_path, set())
    t = templates.TEMPLATES[key]
    Service.model_validate(plan.service)  # a valid service as it is
    for name, text in plan.files.items():
        assert "{port}" not in text, name
        if name.endswith(".py"):
            compile(text, name, "exec")
    ports = plan.service["network"]["ports"]
    assert (ports == []) == (t.port == 0) and plan.service["mode"] == "sandbox"
    assert plan.service["network"]["expose"] == "local" and set(plan.service["env"]) == set(t.env)
    if t.needs in ("npm", "bun"):
        assert plan.steps == [] and '"private": true' in plan.files["package.json"]  # npm install on first Start
    elif t.needs == "python":
        assert plan.steps[0][1:3] == ["-m", "venv"] and "requirements.txt" in plan.files
    else:  # Go, Rust, Deno, Java, PHP, Ruby: their own files, and their own tool fetches what they use
        assert ".gitignore" in plan.files and all(step[0] == f"/bin/{step[0].rsplit('/', 1)[-1]}"
                                                  for step in plan.steps)


@pytest.mark.skipif(not runtimes.which("node"), reason="Node.js isn't installed")
@pytest.mark.parametrize("key", [k for k, t in templates.TEMPLATES.items() if t.needs == "npm" and not t.command])
def test_node_templates_parse(key: str, tmp_path: Path) -> None:
    missing, _how = templates._missing(templates.TEMPLATES[key])
    if missing:  # e.g. Astro on a Node.js older than it supports: refusing that is tested on its own
        pytest.skip(f"{templates.TEMPLATES[key].label} needs {missing}")
    plan = templates.plan(key, "demo", tmp_path, set())
    for name, text in plan.files.items():
        if name.endswith(".js"):
            (tmp_path / name).write_text(text, encoding="utf-8")
            done = subprocess.run([runtimes.which("node") or "node", "--check", str(tmp_path / name)],
                                  capture_output=True, text=True, check=False)
            assert done.returncode == 0, done.stderr


def test_bots_wait_for_their_token(tmp_path: Path) -> None:
    """Without its token a bot says where to put it and exits 0, so it isn't restarted over and over."""
    plan = templates.plan("worker-py", "w", tmp_path, set())
    assert "SIGTERM" in plan.files["worker.py"]
    for key, var in (("discord-py", "DISCORD_TOKEN"), ("telegram-py", "TELEGRAM_TOKEN")):
        text = templates.plan(key, "b", tmp_path, set()).files["bot.py"]
        assert f"No {var} yet" in text and "sys.exit(0)" in text
    webhook = templates.plan("whatsapp-py", "b", tmp_path, set()).files["app.py"]
    assert "compare_digest" in webhook and "X-Hub-Signature-256" in webhook and 'host="127.0.0.1"' in webhook


def test_the_builder_has_one_create(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythonator import sdk
    from tests.test_official_plugins import plugin_module

    monkeypatch.setattr(sdk, "_send", lambda _m: None)
    builder = plugin_module("builder")
    plugin = builder.Builder(permissions=frozenset({"ui", "create_services"}))
    shown: list[dict[str, Any]] = []
    asked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(plugin, "project_templates", lambda: {"templates": [
        {"key": "vue", "label": "Vue", "tool": "create-vue", "port": 5173, "kind": "web", "ready": True,
         "needs": ""}], "folder": "/p"})
    monkeypatch.setattr(plugin, "list_projects", list)
    monkeypatch.setattr(plugin, "show", shown.append)

    def create(*args: Any) -> dict[str, Any]:
        asked.append(args)
        return {"folder": "/p/x", "port": 0}

    monkeypatch.setattr(plugin, "create_project", create)
    plugin.on_start()
    deadline = time.monotonic() + 5
    while plugin.loading and time.monotonic() < deadline:
        time.sleep(0.01)
    plugin.on_action("template", "Vue")
    text = json.dumps(shown[-1])
    assert text.count('"action": "create"') == 1 and "Create my-vue" not in text
    assert "my-vue if empty" in text
    plugin.on_action("create", "")
    deadline = time.monotonic() + 5
    while (plugin.busy or plugin.loading) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert asked == [("vue", "my-vue", "", True)]


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


def test_templates_for_other_runtimes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtimes, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(versions, "installed", lambda prog: None)  # not the runner's own Go 1.24 or PHP
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


def _version(spec: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", spec))


def test_templates_start_from_versions_without_known_advisories() -> None:
    from pythonator.core import template_files, templates

    floors = {"fastify": (5, 12, 8), "hono": (4, 13, 16), "@hono/node-server": (2, 1, 6), "elysia": (1, 4, 31)}
    seen: set[str] = set()
    for template in templates.TEMPLATES.values():
        for package, spec in template.dependencies.items():
            if package in floors:
                seen.add(package)
                assert _version(spec) >= floors[package], (template.key, package, spec)
    assert seen == set(floors)
    assert 'gem "sinatra", "~> 4.2"' in template_files.SINATRA_GEMFILE
    assert 'gem "puma", "~> 8.0", ">= 8.0.2"' in template_files.SINATRA_GEMFILE


def test_express_apps_exit_with_an_error_when_the_port_is_taken() -> None:
    """Express 5 passes a failed listen to the callback: printing "Listening" there and exiting 0 hid it."""
    from pythonator.core import template_files

    for text in (template_files.EXPRESS_JS, template_files.WHATSAPP_JS):
        assert "app.listen(" in text
        callback = text[text.index("app.listen("):]
        assert re.search(r"\(err\) => \{\s*if \(err\) throw err;", callback), callback


def test_bot_templates_stop_cleanly_on_sigterm() -> None:
    assert "add_signal_handler(signal.SIGTERM" in tf.DISCORD_PY and "bot.close()" in tf.DISCORD_PY
    assert 'process.once(signal' in tf.DISCORD_JS and "client.destroy()" in tf.DISCORD_JS
    assert 'process.once("SIGTERM", () => bot.stop())' in tf.TELEGRAM_JS
    compile(tf.DISCORD_PY, "bot.py", "exec")


def test_templates_ask_for_supported_runtimes() -> None:
    from pythonator.core import versions

    floors = {"python": (3, 11), "node": (22,), "go": (1, 26), "ruby": (3, 3), "php": (8, 3), "java": (21,)}
    for t in templates.TEMPLATES.values():
        if not t.engines or t.runtime not in floors or "||" in t.engines:
            continue
        low = versions.parse(t.engines.lstrip(">=^~ ").split()[0]) if hasattr(versions, "parse") else None
        if low is not None:
            assert tuple(low)[:len(floors[t.runtime])] >= floors[t.runtime], (t.key, t.engines)
        else:
            assert not t.engines.startswith((">=3.8", ">=3.9", ">=3.10", ">=20", ">=1.22", ">=8.0", ">=11")), t.key
    discord = templates.TEMPLATES["discord-py"]
    assert "aiohttp>=3.14.5" in discord.requirements
