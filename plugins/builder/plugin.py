"""Project builder: a new React, Vue, Nuxt, FastAPI, Flask or Django project as a service, in one click.

Shows how a plugin uses ``create_services``: it only picks one of Pythonator's built-in templates and a name.
Pythonator itself decides the rest. The framework's official tool (create-vite, create-vue, create-nuxt,
django-admin, or venv and pip with the framework's documented minimal app) runs in the new service's own sandbox,
with write access to the new, empty folder only; the service serves on this computer only. Standard library only.
"""

from __future__ import annotations

import re
import threading
import time

from pythonator.sdk import Plugin, PluginError, ui

NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PICK = "Choose a framework…"


def suggest(base: str, taken: set[str]) -> str:
    """``base`` or ``base-2``, ``base-3``… whichever no service has as its name yet."""
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


class Builder(Plugin):
    def on_start(self) -> None:
        self.templates: list[dict[str, object]] = []
        self.default_folder = ""
        self.chosen = ""
        self.status = ("", "")
        self.made: list[tuple[str, str, str]] = []  # (when, name, folder)
        self.busy = False
        threading.Thread(target=self._load, daemon=True, name="builder-templates").start()
        self.render()

    def _load(self) -> None:
        try:
            data = self.project_templates()
        except PluginError as exc:
            self.status = (f"Can't list the templates: {exc}", "error")
        else:
            self.templates = [t for t in data.get("templates", []) if isinstance(t, dict)]
            self.default_folder = str(data.get("folder", ""))
        self.render()

    def labels(self) -> dict[str, dict[str, object]]:
        return {str(t["label"]): t for t in self.templates}

    def on_action(self, action: str, value: str) -> None:
        if action == "template":
            self.chosen = value if value in self.labels() else ""
            self.status = ("", "")
        elif action == "create":
            self.create(value.strip().lower())
        self.render()

    def on_settings(self) -> None:
        self.render()

    def create(self, name: str) -> None:
        template = self.labels().get(self.chosen)
        if template is None:
            self.status = ("Choose a framework first.", "warn")
            return
        if not template.get("ready"):
            self.status = (f"{self.chosen} needs {template.get('needs')} on this computer.", "warn")
            return
        name = name or suggest(f"my-{template['key']}", {s.name for s in self.services.values()})
        if not NAME.match(name):
            self.status = ("A name is lowercase letters, digits and -, starting with a letter.", "warn")
            return
        if self.busy:
            return
        self.busy = True
        self.status = (f"Creating {name}…", "")
        threading.Thread(target=self._create, args=(str(template["key"]), name), daemon=True,
                         name="builder-create").start()

    def _create(self, key: str, name: str) -> None:
        try:
            done = self.create_project(key, name, str(self.settings.get("folder", "") or ""),
                                       bool(self.settings.get("start", True)))
        except PluginError as exc:
            self.status = (str(exc), "error")
        else:
            self.made.insert(0, (time.strftime("%H:%M"), name, str(done.get("folder", ""))))
            self.status = (f"{name} is being made in {done.get('folder')}: its console shows the tool's output. "
                           f"It serves on http://127.0.0.1:{done.get('port')}/ once started.", "ok")
        finally:
            self.busy = False
            self.render()

    def render(self) -> None:
        if "ui" not in self.permissions:
            return
        options = [PICK, *self.labels()]
        items: list[dict[str, object]] = [
            ui.text("A new project, made by the framework's own tool, added as a service. It runs sandboxed and "
                    "serves on this computer only.", "muted"),
            ui.row(ui.select("template", options, self.chosen or PICK, "Framework")),
        ]
        template = self.labels().get(self.chosen)
        if template is not None:
            items.append(ui.text(f"Made with {template.get('tool')}. Port {template.get('port')} (or the next free "
                                 "one).", "" if template.get("ready") else "warn"))
            if not template.get("ready"):
                items.append(ui.text(f"Install {template.get('needs')} first.", "warn"))
            folder = str(self.settings.get("folder", "") or "") or self.default_folder
            suggested = suggest(f"my-{template.get('key')}", {s.name for s in self.services.values()})
            items.append(ui.input("create", f"Project name (Enter creates it in {folder})"[:200], "Create"))
            if template.get("ready") and not self.busy:
                items.append(ui.row(ui.button(f"Create {suggested}", "create", suggested)))
        if self.status[0]:
            items.append(ui.text(self.status[0], self.status[1]))
        if self.templates:
            rows = [[str(t["label"]), str(t["tool"]), "ready" if t.get("ready") else f"needs {t.get('needs')}"]
                    for t in self.templates]
            items.append(ui.table(["Framework", "Made by", "Here"], rows,
                                  ["" if t.get("ready") else "muted" for t in self.templates]))
        if self.made:
            items.append(ui.table(["Made", "Project", "Folder"], [list(m) for m in self.made[:20]]))
        self.show(ui.panel("Project builder", *items))
