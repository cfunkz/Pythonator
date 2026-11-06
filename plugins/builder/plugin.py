"""Project builder: a new web app, API, bot or worker as a service, in one click; and its projects, to delete.

Shows how a plugin uses ``create_services``: it only picks one of Pythonator's built-in templates and a name.
Pythonator itself decides the rest. The framework's official tool (create-vite, create-vue, create-nuxt,
django-admin, or venv and pip with the library's documented minimal app) runs in the new service's own sandbox,
with write access to the new, empty folder only. Deleting asks you in Pythonator's own window: the plugin can't
delete anything itself. Standard library only.
"""

from __future__ import annotations

import re
import threading

from pythonator.sdk import Plugin, PluginError, ServiceEvent, ui

NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PICK = "Choose a template…"
ORDER = {"web": 0, "bot": 1, "worker": 2}  # web apps and APIs first, then bots, then workers
ALL = "All runtimes"
GROUPS = ("Node.js", "Python", "Go", "Rust", "Bun", "Deno", "Java", "PHP", "Ruby")  # the runtime filter's order
NEW, MADE = "New project", "Projects"  # the panel's two views


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
        self.projects: list[dict[str, object]] = []
        self.default_folder = ""
        self.chosen = ""
        self.group = ALL
        self.picked = ""  # a project, by name, for Delete
        self.status = ("", "")
        self.busy = False
        self.loading = False
        self.page = NEW
        self.refresh()

    # -- what Pythonator has -----------------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.loading:
            self.loading = True
            threading.Thread(target=self._load, daemon=True, name="builder-load").start()
        self.render()

    def _load(self) -> None:
        try:
            data = self.project_templates()
            self.templates = [t for t in data.get("templates", []) if isinstance(t, dict)]
            self.default_folder = str(data.get("folder", ""))
            self.projects = [p for p in self.list_projects() if isinstance(p, dict)]
        except PluginError as exc:
            self.status = (f"Can't list the templates: {exc}", "error")
        finally:
            self.loading = False
            self.render()

    def labels(self) -> dict[str, dict[str, object]]:
        return {str(t["label"]): t for t in self.templates}

    # -- the panel's buttons -----------------------------------------------------------------------------

    def on_action(self, action: str, value: str) -> None:
        if action == "page":
            self.page = MADE if value.startswith(MADE) else NEW
        elif action == "group":
            self.group = value if value in GROUPS else ALL
            if self.chosen and self.group != ALL and self.labels().get(self.chosen, {}).get("group") != self.group:
                self.chosen = ""
        elif action == "template":
            self.chosen = value if value in self.labels() else ""
            self.status = ("", "")
        elif action == "create":
            self.create(value.strip().lower())
        elif action == "refresh":
            self.status = ("", "")
            self.refresh()
            return
        elif action == "project":
            self.picked = value
        elif action == "delete":
            self.delete(value)
        self.render()

    def on_settings(self) -> None:
        self.render()

    def on_event(self, event: ServiceEvent) -> None:
        if event.kind in ("installed", "install_failed", "started", "stopped", "crashed", "exited"):
            self.refresh()  # a project finished being made, or changed state

    def create(self, name: str) -> None:
        template = self.labels().get(self.chosen)
        if template is None:
            self.status = ("Choose a template first.", "warn")
            return
        if not template.get("ready"):
            how = f" ({template.get('how')})" if template.get("how") else ""
            self.status = (f"{self.chosen} needs {template.get('needs')}{how}.", "warn")
            return
        name = name or suggest(f"my-{template['key']}", {s.name for s in self.services.values()})
        if not NAME.match(name):
            self.status = ("A name is lowercase letters, digits and -, starting with a letter.", "warn")
            return
        if self.busy:
            return
        self.busy = True
        self.status = (f"Creating {name}…", "")
        threading.Thread(target=self._create, args=(template, name), daemon=True, name="builder-create").start()

    def _create(self, template: dict[str, object], name: str) -> None:
        try:
            done = self.create_project(str(template["key"]), name, str(self.settings.get("folder", "") or ""),
                                       bool(self.settings.get("start", True)))
        except PluginError as exc:
            self.status = (str(exc), "error")
        else:
            port = int(str(done.get("port") or 0))
            where = f" It serves on http://127.0.0.1:{port}/ once started." if port else ""
            token = " Then fill in its token in Configure → Environment." if template.get("kind") == "bot" else ""
            self.status = (f"{name} is being made in {done.get('folder')}: its console shows the tool's output."
                           f"{where}{token}", "ok")
        finally:
            self.busy = False
            self.refresh()

    def delete(self, name: str) -> None:
        project = next((p for p in self.projects if p.get("name") == name), None)
        if project is None:
            self.status = ("Choose a project to delete.", "warn")
            return
        try:
            self.delete_project(str(project["id"]))
        except PluginError as exc:
            self.status = (str(exc), "error")
        else:
            self.status = (f"Pythonator asks you to confirm deleting {name} in its window.", "")

    # -- the panel ---------------------------------------------------------------------------------------

    def render(self) -> None:
        if "ui" not in self.permissions:
            return
        made = f"{MADE} ({len(self.projects)})" if self.projects else MADE
        items: list[dict[str, object]] = [ui.tabs("page", [NEW, made], made if self.page == MADE else NEW)]
        items += self._new() if self.page == NEW else self._made()
        if self.status[0]:
            items.append(ui.text(self.status[0], self.status[1]))
        self.show(ui.panel("Project builder", *items))

    def _new(self) -> list[dict[str, object]]:
        rank = {g: i for i, g in enumerate(GROUPS)}
        shown = [t for t in self.templates if self.group == ALL or t.get("group") == self.group]
        ordered = sorted(shown, key=lambda t: (ORDER.get(str(t.get("kind")), 9), rank.get(str(t.get("group")), 99)))
        options = [PICK, *(str(t["label"]) for t in ordered)]
        groups = [g for g in GROUPS if any(t.get("group") == g for t in self.templates)]
        items: list[dict[str, object]] = [
            ui.text("A new project, made by the framework's own tool and added as a service. It runs sandboxed, "
                    "on this computer only.", "muted"),
            ui.row(ui.select("group", [ALL, *groups], self.group, "Runtime"),
                   ui.button("Refresh: look for templates and installed tools again", "refresh", icon="refresh")),
            ui.row(ui.select("template", options, self.chosen or PICK, "Template")),
        ]
        template = self.labels().get(self.chosen)
        if template is not None:
            port = template.get("port")
            items.append(ui.text(f"Made with {template.get('tool')}."
                                 + (f" Port {port} (or the next free one)." if port else " It serves no port."),
                                 "" if template.get("ready") else "warn"))
            if not template.get("ready"):
                how = f" ({template.get('how')})" if template.get("how") else ""
                items.append(ui.text(f"It needs {template.get('needs')}{how}. Then press Refresh.", "warn"))
            elif not self.busy:
                folder = str(self.settings.get("folder", "") or "") or self.default_folder
                suggested = suggest(f"my-{template.get('key')}", {s.name for s in self.services.values()})
                # One Create: an empty name takes the suggested one, shown in the box.
                items.append(ui.input("create", f"Name: {suggested} if empty. It goes in {folder}"[:200], "Create"))
        if self.templates:
            ready = sum(1 for t in self.templates if t.get("ready"))
            missing = sorted({str(t.get("needs")) for t in self.templates if not t.get("ready")})
            items.append(ui.text(f"{ready} of {len(self.templates)} templates ready on this computer"
                                 + (f"; the others need {', '.join(missing)}." if missing else "."), "muted"))
        return items

    def _made(self) -> list[dict[str, object]]:
        if not self.projects:
            return [ui.text("Loading…" if self.loading else "No projects made here yet.", "muted"),
                    ui.row(ui.button("Refresh the list", "refresh", icon="refresh"))]
        names = [str(p.get("name", "")) for p in self.projects]
        picked = self.picked if self.picked in names else names[0]
        shown = self.projects[:100]
        return [
            ui.row(ui.select("project", names, picked, "Project"),
                   ui.button(f"Delete {picked}… (Pythonator asks you first)", "delete", picked, icon="delete"),
                   ui.button("Refresh the list", "refresh", icon="refresh")),
            ui.table(["Project", "Template", "State", "Folder"],
                     [[str(p.get("name")), str(p.get("template")), str(p.get("state")), str(p.get("folder"))]
                      for p in shown],
                     ["ok" if p.get("state") == "running" else "error" if p.get("state") == "failed" else ""
                      for p in shown]),
        ]
