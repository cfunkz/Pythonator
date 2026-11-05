# Writing a plugin

A plugin is a folder with two files: `plugin.toml`, which says what it is and what it may do, and a Python file
with one class. This page builds one in five steps. [Plugin API](plugin-api.md) has the full reference, and the three
official plugins in [`plugins/`](../plugins/) are complete examples you can copy.

## 1. Hello, crash

Make a folder `crash-notify` with a `plugin.toml`:

```toml
[plugin]
id = "crash-notify"
name = "Crash notify"
version = "0.1.0"
api = 1
description = "Says so in the window when a service crashes."
permissions = []
```

and a `plugin.py`:

```python
from pythonator.sdk import Plugin, ServiceEvent


class CrashNotify(Plugin):
    def on_event(self, event: ServiceEvent) -> None:
        if event.kind == "crashed":
            self.notify(f"{event.service.name} crashed (exit code {event.exit_code})", "warn")
```

In Pythonator, press **Plugins → Install from folder…** and pick the folder, then tick **Enabled**. You get the
third-party warning, the plugin starts in test mode, and the next crash shows a notice. `notify` and `log` need no
permission.

## 2. Develop without reinstalling

Installing copies the folder. While you work on a plugin, put (or link) its folder straight into the `plugins`
folder of your [data folder](getting-started.md#where-your-data-is-kept) instead, and untick and tick **Enabled** to
restart it with your changes. `print()` and anything your plugin writes to stderr appear under **Activity**, and so
does the traceback when one of your methods raises. The plugin keeps running after an exception.

## 3. Settings

Settings in the manifest become a form in the Plugins window, and arrive in `self.settings`:

```toml
[[plugin.settings]]
key = "only"
label = "Only these services"
kind = "lines"
help = "One name per line. Empty: all of them."

[[plugin.settings]]
key = "sound"
label = "Make it a warning"
kind = "bool"
default = true
```

```python
    def on_event(self, event: ServiceEvent) -> None:
        only = [n.strip() for n in str(self.settings.get("only", "")).splitlines() if n.strip()]
        if event.kind == "crashed" and (not only or event.service.name in only):
            level = "warn" if self.settings.get("sound") else "info"
            self.notify(f"{event.service.name} crashed", level)
```

Kinds are `text`, `secret` (shown as dots, never sent back to the window), `bool`, `int`, `choice` (with
`choices = [...]`) and `lines`. `on_settings()` is called when the user saves.

## 4. A panel

With the `ui` permission, a plugin can show a panel in the window's Plugins dock. Panels are built from a small
design system in `pythonator.sdk.ui`, so every plugin looks like the rest of Pythonator in both themes:

```python
from pythonator.sdk import Plugin, ServiceEvent, ui


class CrashBoard(Plugin):
    def on_start(self) -> None:
        self.crashes: dict[str, int] = {}
        self.render()

    def on_event(self, event: ServiceEvent) -> None:
        if event.kind == "crashed":
            self.crashes[event.service.name] = self.crashes.get(event.service.name, 0) + 1
            self.render()

    def on_action(self, action: str, value: str) -> None:
        if action == "reset":
            self.crashes.clear()
            self.render()

    def render(self) -> None:
        rows = [[name, str(n)] for name, n in sorted(self.crashes.items())]
        self.show(ui.panel(
            "Crashes",
            ui.stat("Total", str(sum(self.crashes.values())), "error" if self.crashes else "ok"),
            ui.table(["Service", "Crashes"], rows) if rows else ui.text("None yet.", "muted"),
            ui.button("Reset", "reset"),
        ))
```

and `permissions = ["ui"]` in the manifest. A button calls `on_action` with its action and value.

## 5. Acting on services

These need permissions, which the user grants:

| You want to | Override or call | Permission |
| --- | --- | --- |
| Refuse a start | return a reason from `check_start(service)` | `policy` |
| Read output | `on_output(service, lines)` | `read_logs` |
| Watch CPU, memory, ports | `on_stats()`, then read `self.services` | `inspect` |
| Start, stop, restart | `self.start(id)`, `self.stop(id)`, `self.restart(id)` | `control` |
| Call a web API | `urllib.request`, `http.client`, `socket`... | `network` |

A policy plugin, refusing services that listen on low ports:

```python
from pythonator.sdk import Plugin, ServiceInfo


class NoLowPorts(Plugin):
    def check_start(self, service: ServiceInfo) -> str | None:
        low = [p for p in service.ports if p < 1024]
        return f"Ports below 1024 aren't allowed: {low}" if low else None
```

`check_start` must answer within 2 seconds; if it doesn't, the start goes ahead without it. Work that takes longer
(network calls, files) belongs in a thread you start in `on_start`, like the official Webhooks and Health checks
plugins do.

## What a plugin can use

A plugin runs with the Python that runs Pythonator, in isolated mode, with its own folder on the import path. It
can use the standard library, `pythonator.sdk`, and pure-Python modules you put in its folder. It can't install
packages. It can write only to `self.data_dir`, a private folder for its own files that Remove deletes.

It runs in the same sandbox as services: no access to your files, no network without the `network` permission, no
other processes.

## Checklist before you share it

* `id` is lowercase, unique, and stays the same forever: settings and grants are kept under it.
* Ask for the fewest permissions that work. Users see each one.
* Keep `check_start` fast and never block in `on_event`, `on_output` or `on_stats`.
* Bump `version` with each change.
* Test it in test mode: its **Activity** shows what it would have done.

## Making it official

Official plugins live in `plugins/` in this repository, are reviewed like any other code, and are recorded by hash
in `pythonator/plugins/official.json`. Open a pull request with the plugin folder and the regenerated catalog:

```bash
python -m pythonator.plugins.trust --update
```

The test suite fails when the catalog doesn't match the plugin folders, so a change can't slip in unreviewed.
