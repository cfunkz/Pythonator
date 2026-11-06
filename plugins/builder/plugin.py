"""Project builder: a new web app, API, bot or worker as a service, in one click; and its projects, to delete.

Shows how a plugin uses ``create_services``: it only picks one of Pythonator's built-in templates and a name.
Pythonator itself decides the rest. The framework's official tool (create-vite, create-vue, create-nuxt,
django-admin, or venv and pip with the library's documented minimal app) runs in the new service's own sandbox,
with write access to the new, empty folder only. Deleting asks you in Pythonator's own window: the plugin can't
delete anything itself. With the "leftovers" setting, Refresh also lists folders in the projects folder that no
service uses any more, to clean up the same way. Standard library only.

Smooth by design: everything that asks Pythonator runs in one background loader, which merges what's wanted while
it runs (ten events in a row are one reload), a service's state change is taken from the event itself without a
request, the templates (which look for tools on PATH) are only looked for again on Refresh, and a panel that
didn't change isn't sent again.
"""

from __future__ import annotations

import json
import re
import threading
import time

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
        self.leftovers: list[dict[str, object]] = []
        self.leftovers_in = ""  # the projects folder they were looked for in; "" not looked yet
        self.default_folder = ""
        self.chosen = ""
        self.group = ALL
        self.picked = ""  # a project, by name, for Delete
        self.status = ("", "")
        self.busy = False
        self.loading = False
        self.page = NEW
        self.lock = threading.Lock()
        self.wanted: set[str] = set()
        self.sent = ""  # the last panel sent, as JSON
        self.follow_until = 0.0  # a clean-up was asked for: the leftovers list follows it for a while
        self.refresh()

    # -- what Pythonator has -----------------------------------------------------------------------------

    def look_for_leftovers(self) -> bool:
        return bool(self.settings.get("leftovers", False))

    def folder(self) -> str:
        return str(self.settings.get("folder", "") or "")

    def refresh(self) -> None:
        """Refresh: the templates (and the tools they need), the projects and, if set, leftover folders."""
        self.load("templates", "projects", *(("leftovers",) if self.look_for_leftovers() else ()))

    def load(self, *what: str) -> None:
        """Ask Pythonator in the background. While a load runs, more wishes join the next round: never two
        loaders, never the same request twice in a row."""
        with self.lock:
            self.wanted.update(what)
            start = not self.loading
            self.loading = True
        if start:
            threading.Thread(target=self._load, daemon=True, name="builder-load").start()
        self.render()

    def _load(self) -> None:
        while True:
            with self.lock:
                what, self.wanted = self.wanted, set()
                if not what:
                    self.loading = False
                    break
            try:
                if "templates" in what:
                    data = self.project_templates()
                    self.templates = [t for t in data.get("templates", []) if isinstance(t, dict)]
                    self.default_folder = str(data.get("folder", ""))
                if "projects" in what:
                    self.projects = [p for p in self.list_projects() if isinstance(p, dict)]
                if "leftovers" in what and self.look_for_leftovers():
                    found = self.leftover_projects(self.folder())
                    self.leftovers = [x for x in found.get("leftovers", []) if isinstance(x, dict)]
                    self.leftovers_in = str(found.get("folder", "")) or "the projects folder"
            except PluginError as exc:
                self.status = (f"Can't ask Pythonator: {exc}", "error")
            self.render()
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
        elif action == "cleanup":
            self.cleanup()
        self.render()

    def on_settings(self) -> None:
        if self.look_for_leftovers():
            self.load("leftovers")  # turned on, or another projects folder: look there
        else:
            self.leftovers, self.leftovers_in = [], ""
        self.render()

    def on_event(self, event: ServiceEvent) -> None:
        project = next((p for p in self.projects if p.get("id") == event.service.id), None)
        if project is not None and project.get("state") != event.service.state:
            project["state"] = event.service.state  # the event says it: no need to ask
            self.render()
        if event.kind in ("installed", "install_failed"):
            self.load("projects")  # a project finished being made

    def on_tick(self) -> None:
        if any(p.get("id") not in self.services for p in self.projects):  # one was removed or deleted
            self.load("projects", *(("leftovers",) if self.look_for_leftovers() else ()))
        elif self.leftovers and self.look_for_leftovers() and time.monotonic() < self.follow_until:
            self.load("leftovers")  # being cleaned up: the list follows

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
            self.load("projects")

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

    def cleanup(self) -> None:
        folders = [str(x.get("folder")) for x in self.leftovers if x.get("folder")]
        if not folders:
            self.status = ("No leftover folders to clean up: press Refresh.", "warn")
            return
        try:
            asked = self.delete_leftovers(folders, self.folder())
        except PluginError as exc:
            self.status = (str(exc), "error")
            self.load("leftovers")
        else:
            self.follow_until = time.monotonic() + 120
            self.status = (f"Pythonator asks you which of the {asked} leftover folders to delete, in its window.", "")

    # -- the panel ---------------------------------------------------------------------------------------

    def render(self) -> None:
        if "ui" not in self.permissions:
            return
        made = f"{MADE} ({len(self.projects)})" if self.projects else MADE
        items: list[dict[str, object]] = [ui.tabs("page", [NEW, made], made if self.page == MADE else NEW)]
        items += self._new() if self.page == NEW else self._made()
        if self.page == NEW and self.leftovers:
            n = len(self.leftovers)
            items.append(ui.text(f"{n} leftover project folder{'s' if n > 1 else ''} found: see {MADE}.", "warn"))
        if self.status[0]:
            items.append(ui.text(self.status[0], self.status[1]))
        panel = ui.panel("Project builder", *items)
        text = json.dumps(panel, sort_keys=True)
        if text != self.sent:  # an unchanged panel isn't sent again (state events come in bursts)
            self.sent = text
            self.show(panel)

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
        refresh = ui.button("Refresh the list" + (" and look for leftover folders" if self.look_for_leftovers()
                                                  else ""), "refresh", icon="refresh")
        if not self.projects:
            items = [ui.text("Loading…" if self.loading else "No projects made here yet.", "muted"), ui.row(refresh)]
        else:
            names = [str(p.get("name", "")) for p in self.projects]
            picked = self.picked if self.picked in names else names[0]
            shown = self.projects[:100]
            items = [
                ui.row(ui.select("project", names, picked, "Project"),
                       ui.button(f"Delete {picked}… (Pythonator asks you first)", "delete", picked, icon="delete"),
                       refresh),
                ui.table(["Project", "Template", "State", "Folder"],
                         [[str(p.get("name")), str(p.get("template")), str(p.get("state")), str(p.get("folder"))]
                          for p in shown],
                         ["ok" if p.get("state") == "running" else "error" if p.get("state") == "failed" else ""
                          for p in shown]),
            ]
        return items + self._leftovers()

    def _leftovers(self) -> list[dict[str, object]]:
        if not self.look_for_leftovers() or not self.leftovers_in:
            return []
        if not self.leftovers:
            return [ui.text(f"No leftover project folders in {self.leftovers_in}.", "muted")]
        n = len(self.leftovers)
        shown = self.leftovers[:100]
        return [
            ui.text(f"{n} folder{'s' if n > 1 else ''} in {self.leftovers_in} no service uses any more: left by "
                    "removed projects.", "warn"),
            ui.table(["Leftover folder", "Made here"],
                     [[str(x.get("name")), "yes" if x.get("made") else "not recorded"] for x in shown]),
            ui.row(ui.button(f"Clean up {n} leftover folder{'s' if n > 1 else ''}… (Pythonator asks you which)",
                             "cleanup")),
        ]
