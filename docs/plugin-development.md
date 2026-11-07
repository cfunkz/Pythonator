# Writing a plugin

A plugin is a folder with two files: `plugin.toml`, which describes the plugin and the permissions it needs, and a
Python file containing one class. This tutorial builds a plugin in five steps. The [Plugin API](plugin-api.md) is the
full reference, and the four official plugins in
[`plugins/`](https://github.com/cfunkz/Pythonator/tree/main/plugins) are complete, working examples.

## 1. A minimal plugin

Create a folder `crash-notify` containing `plugin.toml`:

```toml
[plugin]
id = "crash-notify"
name = "Crash notify"
version = "0.1.0"
api = 5
description = "Shows a notice in the window when a service crashes."
permissions = []
```

and `plugin.py`:

```python
from pythonator.sdk import Plugin, ServiceEvent


class CrashNotify(Plugin):
    def on_event(self, event: ServiceEvent) -> None:
        if event.kind == "crashed":
            self.notify(f"{event.service.name} crashed (exit code {event.exit_code})", "warn")
```

In Pythonator, open **Plugins ▾ → Manage plugins…**, choose **Install from folder…**, select the folder and tick
**Enabled**. The third-party warning appears, the plugin starts in test mode, and the next crash produces a notice.
`notify` and `log` need no permissions.

## 2. Developing without reinstalling

Installing copies the folder. During development, place (or symlink) the folder directly in `plugins/` inside the
[data folder](getting-started.md#the-data-folder) instead, and untick and re-tick **Enabled** to restart it with the
latest changes. `print()` output and anything written to stderr appear under **Activity**, as does the traceback when
a method raises. The plugin keeps running after an exception.

## 3. Settings

Settings declared in the manifest become a form in the Plugins window, and their values arrive in `self.settings`:

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

The kinds are `text`, `secret` (shown as dots, stored in the system keychain and never sent back to the window),
`bool`, `int`, `choice` (with `choices = [...]`) and `lines`. `on_settings()` is called when the user saves.

## 4. A panel

With the `ui` permission, a plugin can show a panel in the Plugins dock. Panels are built from the elements in
`pythonator.sdk.ui`, so every plugin matches the rest of the window in both themes:

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
            ui.button("Reset", "reset", icon="refresh"),
        ))
```

Add `permissions = ["ui"]` to the manifest. A button calls `on_action` with its action and value. The full list of
elements, and how they are sized, is under [Panels](plugin-api.md#panels).

## 5. Acting on services

The following require permissions, granted by the user:

| Goal | Override or call | Permission |
| --- | --- | --- |
| Refuse a start | Return a reason from `check_start(service)` | `policy` |
| Read output | `on_output(service, lines)` | `read_logs` |
| Monitor CPU, memory and ports | `on_stats()`, then read `self.services` | `inspect` |
| Start, stop, restart | `self.start(id)`, `self.stop(id)`, `self.restart(id)` | `control` |
| Call a web API | `urllib.request`, `http.client`, `socket`, … | `network` |

A policy plugin that refuses services listening on privileged ports:

```python
from pythonator.sdk import Plugin, ServiceInfo


class NoLowPorts(Plugin):
    def check_start(self, service: ServiceInfo) -> str | None:
        low = [p for p in service.ports if p < 1024]
        return f"Ports below 1024 aren't allowed: {low}" if low else None
```

`check_start` must answer within 2 seconds, or the start goes ahead without it. Slower work (network calls, file
access) belongs in a thread started from `on_start`, as in the official
[Notify](https://github.com/cfunkz/Pythonator/blob/main/plugins/notify/plugin.py) plugin.

## Runtime environment

A plugin runs with the Python interpreter that runs Pythonator, in [isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I),
with its own folder on the import path. It can use the standard library, `pythonator.sdk`, and pure-Python modules
placed in its folder; it cannot install packages. It can write only to `self.data_dir`, a private folder that is
deleted when the plugin is removed.

It runs in the same sandbox as services: no access to the user's files, no network without the `network`
permission, and no other processes.

## Before publishing

* Choose an `id` that is lowercase and unique, and never change it: settings and grants are stored under it.
* Request the fewest permissions that work. Users see each one.
* Keep `check_start` fast, and never block in `on_event`, `on_output` or `on_stats`.
* Increment `version` with each change.
* Test in test mode: **Activity** shows what the plugin would have done.

## Making a plugin official

Official plugins live in `plugins/` in this repository, are reviewed like any other code, and are recorded by hash in
[`pythonator/plugins/official.json`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/plugins/official.json).
Open a pull request with the plugin folder and the regenerated catalogue:

```bash
python -m pythonator.plugins.trust --update
```

The test suite fails if the catalogue does not match the plugin folders, so a change cannot be merged unreviewed.
See [Development and CI](development.md#official-plugins).
