"""New projects from each framework's official tool, as services: what the Project builder plugin can ask for.

The plugin only picks a template and a name. Everything else is decided here, in Pythonator: the exact command
(the framework's own scaffolder, pinned to a major version, or Python's venv and pip with the library's
documented minimal app, as for the bots in :mod:`.template_files`), the new folder (which must not exist yet),
and the service (Sandbox mode, this computer only). The scaffolder runs as a setup step in the new service's own
sandbox, with the network on and write access to that one new folder only, like Install packages. Its packages
are installed by the first Start (Own environment), in the same sandbox.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import IS_WINDOWS
from . import runtimes, versions
from . import template_files as tf

NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")  # a folder name, an npm package name and (with _) a Python module

FLASK_APP = '''"""A Flask app, as in Flask's quickstart (flask.palletsprojects.com/quickstart). Pythonator runs it with
``python app.py`` and sets PORT; ``flask --app app run`` works too."""

import os

from flask import Flask

app = Flask(__name__)


@app.route("/")
def hello_world():
    return "<p>Hello, World!</p>"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "{port}")))
'''

FASTAPI_APP = '''"""A FastAPI app, as in FastAPI's first steps (fastapi.tiangolo.com/tutorial/first-steps).
Pythonator runs it with ``python main.py`` (Uvicorn, which ``fastapi[standard]`` brings) and sets PORT;
``fastapi dev main.py`` works too. The interactive docs are at /docs."""

import os

import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "{port}")))
'''

GITIGNORE = ".venv/\n__pycache__/\n*.pyc\n.env\n"


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    label: str
    tool: str  # what makes it, in words
    needs: str  # the program it needs on PATH: a key of NEEDS
    port: int  # 0: it serves no port (a bot that connects out)
    runtime: str
    entry: str
    args: tuple[str, ...] = ()
    requirements: str = ""  # Python: requirements.txt
    files: dict[str, str] = field(default_factory=dict)  # the documented minimal app
    command: tuple[str, ...] = ()  # node: the scaffolder, after "npm"; {name} is the project's name
    dependencies: dict[str, str] = field(default_factory=dict)  # node and bun without a scaffolder: package.json's
    env: tuple[str, ...] = ()  # variables it needs, put empty in its Environment table for you to fill in
    kind: str = "web"  # "web", "bot" or "worker", for the plugin's list
    engines: str = ""  # node: the Node.js versions the framework supports, as its package.json says (npm ranges)
    group: str = ""  # the plugin's list: "Node.js", "Python", "Go"…
    serve: tuple[str, ...] = ()  # runtime "custom": the command after the program; {port} is the service's port
    setup: tuple[tuple[str, ...], ...] = ()  # steps after the files are written, after the program


# The program a template needs on PATH, and how the plugin names it when it's missing.
NEEDS: dict[str, tuple[tuple[str, ...], str]] = {
    "npm": (("npm",), "Node.js (npm)"),
    "python": (runtimes.RUNTIMES["python"].programs, "Python 3"),
    "go": (("go",), "Go"),
    "cargo": (("cargo",), "Rust (cargo)"),
    "bun": (("bun",), "Bun"),
    "deno": (("deno",), "Deno"),
    "java": (("java",), "Java 21 (a JDK)"),
    "php": (("php",), "PHP 8"),
    "bundle": (("bundle",), "Ruby with Bundler"),
}

VITE = ("--host", "127.0.0.1", "--port", "{port}", "--strictPort")
VITE_NODE = "^20.19.0 || >=22.12.0"  # create-vite 9 and Vite 8
NODE_LTS = ">=20"


def _vite(key: str, label: str, template: str) -> Template:
    return Template(key, label, f"create-vite, Vite's official tool (npm create vite@9, {template} template)",
                    "npm", 5173, "node", "node_modules/vite/bin/vite.js", VITE,
                    command=("create", "vite@9", "{name}", "--", "--template", template, "--no-interactive"),
                    engines=VITE_NODE, group="Node.js")


def _node(key: str, label: str, tool: str, files: dict[str, str], deps: dict[str, str], *, port: int = 3000,
          kind: str = "web", env: tuple[str, ...] = ()) -> Template:
    return Template(key, label, tool, "npm", port, "node", "index.js", files=files, dependencies=deps,
                    env=env, kind=kind, engines=NODE_LTS, group="Node.js")


def _python(key: str, label: str, tool: str, requirements: str, files: dict[str, str], *, port: int = 0,
            engines: str = ">=3.10") -> Template:
    return Template(key, label, tool, "python", port, "python", next(iter(files)), requirements=requirements,
                    files=files, engines=engines, group="Python")


TEMPLATES: dict[str, Template] = {t.key: t for t in (
    # Node.js: front ends made by each framework's official tool, served by its dev server
    _vite("react", "React (Vite)", "react"),
    _vite("react-ts", "React + TypeScript (Vite)", "react-ts"),
    Template("vue", "Vue (Vite)", "create-vue, Vue's official tool (npm create vue@3, its defaults)",
             "npm", 5173, "node", "node_modules/vite/bin/vite.js", VITE,
             command=("create", "vue@3", "{name}", "--", "--default"), engines="^22.18.0 || >=24.12.0",
             group="Node.js"),
    _vite("svelte", "Svelte (Vite)", "svelte"),
    _vite("solid", "Solid (Vite)", "solid"),
    _vite("preact", "Preact (Vite)", "preact"),
    _vite("vanilla", "Vanilla JavaScript (Vite)", "vanilla"),
    Template("nuxt", "Nuxt", "create-nuxt, Nuxt's official tool (npm create nuxt@4, minimal template)",
             "npm", 3000, "node", "node_modules/nuxt/bin/nuxt.mjs",
             ("dev", "--host", "127.0.0.1", "--port", "{port}", "--no-fork"),  # one process, one port
             command=("create", "nuxt@4", "{name}", "--", "--packageManager", "npm", "--gitInit=false",
                      "--no-install", "--template", "minimal"), engines="^22.22.3 || ^24.15.0 || >=26.0.0",
             group="Node.js"),
    Template("next", "Next.js", "create-next-app, Next.js's official tool (npm create next-app@16, its defaults)",
             "npm", 3000, "node", "node_modules/next/dist/bin/next", ("dev", "-H", "127.0.0.1", "-p", "{port}"),
             command=("create", "next-app@16", "{name}", "--", "--yes", "--use-npm", "--skip-install",
                      "--disable-git"), engines=">=20.9.0", group="Node.js"),
    Template("astro", "Astro", "Astro's minimal starter (astro 7, as npm create astro makes it)",
             "npm", 4321, "node", "node_modules/astro/bin/astro.mjs",
             ("dev", "--host", "127.0.0.1", "--port", "{port}", "--ignore-lock"),
             dependencies={"astro": "^7.4.7"},
             files={"astro.config.mjs": tf.ASTRO_CONFIG, "src/pages/index.astro": tf.ASTRO_INDEX,
                    "tsconfig.json": tf.ASTRO_TSCONFIG},
             engines=">=22.23.4", group="Node.js"),
    Template("angular", "Angular", "the Angular CLI, Angular's official tool (ng new, from @angular/cli 22)",
             "npm", 4200, "node", "node_modules/@angular/cli/bin/ng.js",
             ("serve", "--host", "127.0.0.1", "--port", "{port}"),
             command=("exec", "--yes", "--", "@angular/cli@22", "new", "{name}", "--defaults", "--skip-install",
                      "--skip-git", "--ssr=false", "--ai-config=none"),
             engines="^22.22.3 || ^24.15.0 || >=26.0.0", group="Node.js"),
    # Node.js: APIs, each the framework's documented hello-world app
    _node("express", "Express API", "Express 5 and its hello-world app", {"index.js": tf.EXPRESS_JS},
          {"express": "^5.3.0"}),
    _node("fastify", "Fastify API", "Fastify 5 and its getting-started app", {"index.js": tf.FASTIFY_JS},
          {"fastify": "^5.12.0"}),
    _node("hono", "Hono API (Node.js)", "Hono 4 on @hono/node-server", {"index.js": tf.HONO_JS},
          {"hono": "^4.13.0", "@hono/node-server": "^2.1.0"}),
    _node("koa", "Koa API", "Koa 3 and its hello-world app", {"index.js": tf.KOA_JS}, {"koa": "^3.3.0"}),
    # Python
    _python("fastapi", "FastAPI", "venv and pip with fastapi[standard], and FastAPI's first-steps app",
            "fastapi[standard]>=0.162,<1\n", {"main.py": FASTAPI_APP}, port=8000),
    _python("flask", "Flask", "venv and pip with Flask, and Flask's quickstart app", "Flask>=3.1.3,<4\n",
            {"app.py": FLASK_APP}, port=5050),
    Template("django", "Django", "venv and pip with Django 5.2 LTS, then django-admin startproject (Django's own tool)",
             "python", 8000, "python", "manage.py", ("runserver", "127.0.0.1:{port}"),
             requirements="Django>=5.2.19,<6\n", engines=">=3.10", group="Python"),
    _python("litestar", "Litestar API", "venv and pip with Litestar and Uvicorn, and a hello-world app",
            "litestar>=2.25,<3\nuvicorn>=0.54\n", {"app.py": tf.LITESTAR_PY}, port=8000),
    _python("streamlit", "Streamlit data app", "venv and pip with Streamlit, and a small app",
            "streamlit>=1.67,<2\n", {"app.py": tf.STREAMLIT_PY}, port=8501, engines=">=3.11"),
    _python("gradio", "Gradio app", "venv and pip with Gradio, and its hello-world interface",
            "gradio>=6.31,<7\n", {"app.py": tf.GRADIO_PY}, port=7860),
    # Other runtimes
    Template("go", "Go web server", "the Go standard library (net/http): nothing to install", "go", 8080, "go",
             ".", files={"go.mod": tf.GO_MOD, "main.go": tf.GO_HTTP}, engines=">=1.22", group="Go"),
    Template("gin", "Gin API (Go)", "Gin and its quickstart app (go mod tidy fetches it)", "go", 8080, "go", ".",
             files={"go.mod": tf.GO_MOD, "main.go": tf.GIN_GO}, setup=(("mod", "tidy"),), engines=">=1.22",
             group="Go"),
    Template("axum", "Axum API (Rust)", "Axum 0.8 on Tokio (the first start compiles it: a minute or two)",
             "cargo", 3000, "rust", "", files={"Cargo.toml": tf.AXUM_TOML, "src/main.rs": tf.AXUM_RS}, engines=">=1.85",
             group="Rust"),
    Template("bun-hono", "Hono API (Bun)", "Hono 4 on Bun.serve", "bun", 3000, "bun", "index.ts",
             files={"index.ts": tf.HONO_BUN_TS}, dependencies={"hono": "^4.13.0"}, engines=">=1.1", group="Bun"),
    Template("elysia", "Elysia API (Bun)", "Elysia 1 and its hello-world app", "bun", 3000, "bun", "index.ts",
             files={"index.ts": tf.ELYSIA_TS}, dependencies={"elysia": "^1.4.0"}, engines=">=1.2", group="Bun"),
    Template("deno", "Deno web server", "Deno.serve: nothing to install", "deno", 8000, "deno", "main.ts",
             files={"main.ts": tf.DENO_TS, "deno.json": tf.DENO_JSON}, engines=">=2", group="Deno"),
    Template("java", "Java web server", "the JDK's own HttpServer, one file run as is (java Main.java)", "java",
             8080, "java", "Main.java", files={"Main.java": tf.JAVA_MAIN}, engines=">=11", group="Java"),
    Template("php", "PHP site", "PHP's built-in web server (php -S) and an index.php", "php", 8000,
             runtimes.CUSTOM, "", files={"public/index.php": tf.PHP_INDEX},
             serve=("-S", "127.0.0.1:{port}", "-t", "public"), engines=">=8.0", group="PHP"),
    Template("sinatra", "Sinatra (Ruby)", "Bundler with Sinatra and Puma, and a hello-world app", "bundle", 4567,
             "ruby", "app.rb", files={"Gemfile": tf.SINATRA_GEMFILE, "app.rb": tf.SINATRA_RB}, engines=">=3.0",
             group="Ruby"),
    # Bots and workers
    Template("discord-py", "Discord bot (Python)", "venv and pip with discord.py, and a /ping bot",
             "python", 0, "python", "bot.py", requirements="discord.py>=2.7,<3\n", files={"bot.py": tf.DISCORD_PY},
             env=("DISCORD_TOKEN",), kind="bot", engines=">=3.8", group="Python"),
    _node("discord-js", "Discord bot (Node.js)", "discord.js 14 and a /ping bot", {"index.js": tf.DISCORD_JS},
          {"discord.js": "^14.28.1"}, port=0, kind="bot", env=("DISCORD_TOKEN",)),
    Template("telegram-py", "Telegram bot (Python)", "venv and pip with python-telegram-bot, and an echo bot",
             "python", 0, "python", "bot.py", requirements="python-telegram-bot>=22.8,<23\n",
             files={"bot.py": tf.TELEGRAM_PY}, env=("TELEGRAM_TOKEN",), kind="bot", engines=">=3.10", group="Python"),
    _node("telegram-js", "Telegram bot (Node.js)", "grammY and an echo bot", {"index.js": tf.TELEGRAM_JS},
          {"grammy": "^1.46.0"}, port=0, kind="bot", env=("TELEGRAM_TOKEN",)),
    Template("whatsapp-py", "WhatsApp bot (Python)", "Flask and a webhook for Meta's WhatsApp Cloud API",
             "python", 5060, "python", "app.py", requirements="Flask>=3.1.3,<4\n", files={"app.py": tf.WHATSAPP_PY},
             env=("WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET"), kind="bot", engines=">=3.9",
             group="Python"),
    _node("whatsapp-js", "WhatsApp bot (Node.js)", "Express and a webhook for Meta's WhatsApp Cloud API",
          {"index.js": tf.WHATSAPP_JS}, {"express": "^5.3.0"}, port=3010, kind="bot",
          env=("WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET")),
    Template("worker-py", "Python worker", "a venv and a script that does a job every minute, and stops cleanly",
             "python", 0, "python", "worker.py", files={"worker.py": tf.WORKER_PY}, env=("INTERVAL",),
             kind="worker", engines=">=3.9", group="Python"),
    _node("worker-js", "Node.js worker", "a script that does a job every minute, and stops cleanly",
          {"index.js": tf.WORKER_JS}, {}, port=0, kind="worker", env=("INTERVAL",)),
)}


def catalog() -> list[dict[str, Any]]:
    """For the plugin: each template, and whether this computer has what it needs (the program, and a Node.js
    the framework supports)."""
    out = []
    checked: dict[tuple[str, str], tuple[str, str]] = {}  # many templates need the same program: look once
    for t in TEMPLATES.values():
        if (t.needs, t.engines) not in checked:
            checked[(t.needs, t.engines)] = _missing(t)
        missing, how = checked[(t.needs, t.engines)]
        out.append({"key": t.key, "label": t.label, "tool": t.tool, "port": t.port, "kind": t.kind,
                    "group": t.group, "ready": not missing, "needs": missing, "how": how})
    return out


def _program(t: Template) -> str | None:
    return next((p for name in NEEDS[t.needs][0] if (p := runtimes.which(name))), None)


# The runtime whose version a template's needs program reports, for :mod:`.versions`.
VERSION_OF = {"npm": "node", "python": "python", "go": "go", "cargo": "rust", "bun": "bun", "deno": "deno",
              "java": "java", "php": "php", "bundle": "ruby"}


def _missing(t: Template) -> tuple[str, str]:
    """What this computer lacks for ``t`` and how to get it (("", "") when nothing): the program, or a version
    of its runtime the framework supports."""
    prog = _program(t)
    if not prog:
        return NEEDS[t.needs][1], ""
    if not t.engines:
        return "", ""
    runtime = VERSION_OF[t.needs]
    program = runtimes.which("node") if runtime == "node" else runtimes.which("ruby") if runtime == "ruby" else prog
    return versions.shortfall(runtime, versions.installed(program) if program else None, t.engines)


def default_parent() -> Path:
    return Path.home() / "Pythonator Projects"


def free_port(preferred: int, taken: set[int]) -> int:
    """``preferred`` or the next port no service uses and nothing on this computer listens on."""
    port = preferred
    while port < 65000:
        if port not in taken and _free(port):
            return port
        port += 1
    return preferred


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@dataclass
class Plan:
    """What :func:`plan` decided: the service to add, the steps to run in its sandbox, the files to write."""

    service: dict[str, Any]
    steps: list[list[str]]
    files: dict[str, str]
    flatten: str = ""  # a node scaffolder's folder to move up into the project folder afterwards


def plan(key: str, name: str, parent: Path, taken_ports: set[int]) -> Plan:
    """Raises ValueError with what is wrong (unknown template, bad name, folder exists, tool missing, a Node.js
    the framework doesn't support)."""
    t = TEMPLATES.get(key)
    if t is None:
        raise ValueError("Unknown template")
    if not NAME.match(name):
        raise ValueError("A project name is lowercase letters, digits and -, starting with a letter (at most 40)")
    folder = parent / name
    if folder.exists():
        raise ValueError(f"{folder} exists already: pick another name")
    prog = _program(t)
    missing, how = _missing(t)
    if missing:
        raise ValueError(f"{t.label} needs {missing}" + (f": {how}" if how else ""))
    assert prog is not None  # noqa: S101  # _missing says so otherwise
    port = free_port(t.port, taken_ports) if t.port else 0
    service: dict[str, Any] = {
        "name": name, "folder": str(folder), "runtime": t.runtime, "entry": t.entry,
        "args": [a.replace("{port}", str(port)) for a in t.args], "mode": "sandbox", "own_env": True,
        "network": {"enabled": True, "ports": [port] if port else [], "expose": "local"}, "restart": "on-failure",
        "env": dict.fromkeys(t.env, ""),
    }
    if t.serve:  # a program's own server (php -S): the whole command line
        service["command"] = [prog, *(a.replace("{port}", str(port)) for a in t.serve)]
    module = name.replace("-", "_")
    files = {path: text.replace("{port}", str(port)).replace("{name}", name).replace("{module}", module)
             for path, text in t.files.items()}
    steps: list[list[str]] = []
    flatten = ""
    if t.command:  # the framework's own scaffolder makes the project
        steps.append([prog, *(a.replace("{name}", name) for a in t.command)])
        flatten = name
    elif t.needs in ("npm", "bun"):  # its packages are installed by the first Start (npm install), in its sandbox
        scripts = ({"dev": "astro dev", "build": "astro build", "preview": "astro preview", "astro": "astro"}
                   if t.key == "astro" else None)
        files["package.json"] = tf.package_json(name, t.dependencies, t.entry, bun=t.needs == "bun",
                                                engines=t.engines or NODE_LTS, scripts=scripts)
        files[".gitignore"] = "node_modules/\n.env\n" + ("dist/\n.astro/\n" if t.key == "astro" else "")
    elif t.needs == "python":
        files["requirements.txt"] = t.requirements
        files[".gitignore"] = GITIGNORE
        steps += _python_steps(folder, prog)
        if t.key == "django":
            py = str(runtimes.venv_python(folder / ".venv"))
            steps.append([py, "-m", "django", "startproject", module, "."])
    else:
        files[".gitignore"] = OTHER_IGNORE.get(t.runtime, ".env\n")
    steps += [[prog, *step] for step in t.setup]
    return Plan(service, steps, files, flatten)


OTHER_IGNORE = {"rust": "target/\n.env\n", "ruby": "vendor/bundle/\n.bundle/\n.env\n", "java": "*.class\n.env\n"}


def _python_steps(folder: Path, prog: str) -> list[list[str]]:
    """A .venv and the requirements, as Install packages makes them (``requirements.txt`` is written first)."""
    venv = folder / ".venv"
    py = str(runtimes.venv_python(venv))
    return [[prog, "-m", "venv", "--without-pip", str(venv)], [py, "-m", "ensurepip", "--upgrade", "--default-pip"],
            [py, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]]


def write_files(folder: Path, files: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=False)
    for rel, text in files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\r\n" if IS_WINDOWS else "\n")


def flatten(folder: Path, sub: str) -> None:
    """Move what a scaffolder made in ``folder/sub`` up into ``folder`` (links are moved as links, never
    followed), then remove the empty ``sub``."""
    source = folder / sub
    if not source.is_dir() or source.is_symlink():
        raise OSError(f"the tool didn't make {sub}")
    for entry in list(source.iterdir()):
        target = folder / entry.name
        if target.exists() or target.is_symlink():
            raise OSError(f"{entry.name} is in the way")
        shutil.move(str(entry), str(target))
    with contextlib.suppress(OSError):
        source.rmdir()
