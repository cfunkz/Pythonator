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
from . import runtimes
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
    needs: str  # the program it needs on PATH: "npm" or "python"
    port: int  # 0: it serves no port (a bot that connects out)
    runtime: str
    entry: str
    args: tuple[str, ...] = ()
    requirements: str = ""  # Python: requirements.txt
    files: dict[str, str] = field(default_factory=dict)  # the documented minimal app
    command: tuple[str, ...] = ()  # node: the scaffolder, after "npm"; {name} is the project's name
    dependencies: dict[str, str] = field(default_factory=dict)  # node without a scaffolder: package.json's
    env: tuple[str, ...] = ()  # variables it needs, put empty in its Environment table for you to fill in
    kind: str = "web"  # "web", "bot" or "worker", for the plugin's list


VITE = ("--host", "127.0.0.1", "--port", "{port}", "--strictPort")

TEMPLATES: dict[str, Template] = {t.key: t for t in (
    Template("react", "React (Vite)", "create-vite, Vite's official tool (npm create vite@9, React template)",
             "npm", 5173, "node", "node_modules/vite/bin/vite.js", VITE,
             command=("create", "vite@9", "{name}", "--", "--template", "react", "--no-interactive")),
    Template("vue", "Vue (Vite)", "create-vue, Vue's official tool (npm create vue@3, its defaults)",
             "npm", 5173, "node", "node_modules/vite/bin/vite.js", VITE,
             command=("create", "vue@3", "{name}", "--", "--default")),
    Template("nuxt", "Nuxt", "create-nuxt, Nuxt's official tool (npm create nuxt@4, minimal template)",
             "npm", 3000, "node", "node_modules/nuxt/bin/nuxt.mjs", ("dev", "--host", "127.0.0.1", "--port", "{port}"),
             command=("create", "nuxt@4", "{name}", "--", "--packageManager", "npm", "--gitInit=false",
                      "--no-install", "--template", "minimal")),
    Template("express", "Express API (Node.js)", "Express 5 and its hello-world app",
             "npm", 3000, "node", "index.js", files={"index.js": tf.EXPRESS_JS}, dependencies={"express": "^5.3.0"}),
    Template("fastapi", "FastAPI", "venv and pip with fastapi[standard], and FastAPI's first-steps app",
             "python", 8000, "python", "main.py", requirements="fastapi[standard]>=0.162,<1\n",
             files={"main.py": FASTAPI_APP}),
    Template("flask", "Flask", "venv and pip with Flask, and Flask's quickstart app",
             "python", 5050, "python", "app.py", requirements="Flask>=3.1.3,<4\n", files={"app.py": FLASK_APP}),
    Template("django", "Django", "venv and pip with Django 5.2 LTS, then django-admin startproject (Django's own tool)",
             "python", 8000, "python", "manage.py", ("runserver", "127.0.0.1:{port}"),
             requirements="Django>=5.2.19,<6\n"),
    Template("discord-py", "Discord bot (Python)", "venv and pip with discord.py, and a /ping bot",
             "python", 0, "python", "bot.py", requirements="discord.py>=2.7,<3\n", files={"bot.py": tf.DISCORD_PY},
             env=("DISCORD_TOKEN",), kind="bot"),
    Template("discord-js", "Discord bot (Node.js)", "discord.js 14 and a /ping bot",
             "npm", 0, "node", "index.js", files={"index.js": tf.DISCORD_JS},
             dependencies={"discord.js": "^14.28.1"}, env=("DISCORD_TOKEN",), kind="bot"),
    Template("telegram-py", "Telegram bot (Python)", "venv and pip with python-telegram-bot, and an echo bot",
             "python", 0, "python", "bot.py", requirements="python-telegram-bot>=22.8,<23\n",
             files={"bot.py": tf.TELEGRAM_PY}, env=("TELEGRAM_TOKEN",), kind="bot"),
    Template("telegram-js", "Telegram bot (Node.js)", "grammY and an echo bot",
             "npm", 0, "node", "index.js", files={"index.js": tf.TELEGRAM_JS}, dependencies={"grammy": "^1.46.0"},
             env=("TELEGRAM_TOKEN",), kind="bot"),
    Template("whatsapp-py", "WhatsApp bot (Python)", "Flask and a webhook for Meta's WhatsApp Cloud API",
             "python", 5060, "python", "app.py", requirements="Flask>=3.1.3,<4\n", files={"app.py": tf.WHATSAPP_PY},
             env=("WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET"), kind="bot"),
    Template("whatsapp-js", "WhatsApp bot (Node.js)", "Express and a webhook for Meta's WhatsApp Cloud API",
             "npm", 3010, "node", "index.js", files={"index.js": tf.WHATSAPP_JS}, dependencies={"express": "^5.3.0"},
             env=("WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET"), kind="bot"),
    Template("worker-py", "Python worker", "a venv and a script that does a job every minute, and stops cleanly",
             "python", 0, "python", "worker.py", files={"worker.py": tf.WORKER_PY}, env=("INTERVAL",),
             kind="worker"),
)}


def catalog() -> list[dict[str, Any]]:
    """For the plugin: each template, and whether this computer has what it needs."""
    out = []
    for t in TEMPLATES.values():
        prog = _program(t)
        out.append({"key": t.key, "label": t.label, "tool": t.tool, "port": t.port, "kind": t.kind, "ready": bool(prog),
                    "needs": "" if prog else ("Node.js (npm)" if t.needs == "npm" else "Python 3")})
    return out


def _program(t: Template) -> str | None:
    if t.needs == "npm":
        return runtimes.which("npm")
    return next((p for name in runtimes.RUNTIMES["python"].programs if (p := runtimes.which(name))), None)


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
    """Raises ValueError with what is wrong (unknown template, bad name, folder exists, tool missing)."""
    t = TEMPLATES.get(key)
    if t is None:
        raise ValueError("Unknown template")
    if not NAME.match(name):
        raise ValueError("A project name is lowercase letters, digits and -, starting with a letter (at most 40)")
    folder = parent / name
    if folder.exists():
        raise ValueError(f"{folder} exists already: pick another name")
    prog = _program(t)
    if not prog:
        raise ValueError(f"{t.label} needs {'Node.js (npm)' if t.needs == 'npm' else 'Python 3'} on this computer")
    port = free_port(t.port, taken_ports) if t.port else 0
    service: dict[str, Any] = {
        "name": name, "folder": str(folder), "runtime": t.runtime, "entry": t.entry,
        "args": [a.replace("{port}", str(port)) for a in t.args], "mode": "sandbox", "own_env": True,
        "network": {"enabled": True, "ports": [port] if port else [], "expose": "local"}, "restart": "on-failure",
        "env": dict.fromkeys(t.env, ""),
    }
    files = {path: text.replace("{port}", str(port)) for path, text in t.files.items()}
    steps: list[list[str]] = []
    flatten = ""
    if t.command:  # the framework's own scaffolder makes the project
        steps.append([prog, *(a.replace("{name}", name) for a in t.command)])
        flatten = name
    elif t.needs == "npm":  # its packages are installed by the first Start (npm install), in its sandbox
        files["package.json"] = tf.package_json(name, t.dependencies, t.entry)
        files[".gitignore"] = "node_modules/\n.env\n"
    else:
        files["requirements.txt"] = t.requirements
        files[".gitignore"] = GITIGNORE
        steps += _python_steps(folder, prog)
        if t.key == "django":
            py = str(runtimes.venv_python(folder / ".venv"))
            steps.append([py, "-m", "django", "startproject", name.replace("-", "_"), "."])
    return Plan(service, steps, files, flatten)


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
