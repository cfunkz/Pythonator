# Plugin API

Reference for everything a plugin can declare, receive and do, in plugin API version **5** (Pythonator 0.0.13 and
later). Plugins written for an older API version run unchanged. [Writing a plugin](plugin-development.md) is a
step-by-step introduction. The SDK itself is a single standard-library module,
[`pythonator/sdk.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/sdk.py).

## How a plugin runs

```text
Pythonator's background service                 the plugin's own process (sandboxed)
┌──────────────────────────────┐   JSON lines   ┌───────────────────────────────────┐
│ plugin host                  │ ─────────────▶ │ worker: loads plugin.py,          │
│  · checks every message      │   stdin        │ calls the plugin's on_* methods   │
│  · applies permissions       │ ◀───────────── │                                   │
│  · rate limit, size limit    │   stdout       │ self.notify(), self.show(), ...   │
└──────────────────────────────┘                └───────────────────────────────────┘
```

Pythonator starts `Pythonator --plugin-worker <plugin folder> <data folder>` in the same kind of sandbox that
services use, with network access only if the plugin was granted `network`. The worker
([`plugins/worker.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/plugins/worker.py)) imports the entry
file (compiling it from source and ignoring any shipped `.pyc` files), sends `hello`, and then calls the plugin's
methods as messages arrive, one at a time.

Plugin code never sees Pythonator's own objects, only copies of the data its permissions allow. The host side is
[`plugins/host.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/plugins/host.py).

## plugin.toml

```toml
[plugin]
id = "my-plugin"            # required: a-z, 0-9 and -, 2 to 41 characters, starting with a letter
name = "My plugin"          # required: shown everywhere, up to 64 characters
version = "1.2.0"           # required: 1 to 4 numbers, optional suffix (1.2.0-beta1)
api = 5                     # the plugin API it was written for; a newer one than Pythonator's is refused
entry = "plugin.py"         # the Python file, inside the plugin folder
description = "..."         # up to 300 characters
permissions = ["network"]   # see below
author = "Your Name"
homepage = "https://..."

[[plugin.settings]]         # any number, up to 40
key = "interval"            # a-z, 0-9 and _, starting with a letter
label = "Seconds between checks"
kind = "int"                # text, secret, bool, int, choice, lines
default = 30
choices = []                # for kind = "choice"
help = "Shown under the field."
```

The file must be under 64 KB and is parsed as [TOML](https://toml.io/en/v1.0.0). Unknown keys are refused, so a typo
is an error rather than being silently ignored. Reading the manifest imports nothing from the plugin. The schema is
`Manifest` in [`plugins/manifest.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/plugins/manifest.py).

## Permissions

| Name | Gives |
| --- | --- |
| `network` | Network in the plugin's sandbox |
| `read_logs` | `on_output` calls with the services' output |
| `inspect` | `cpu`, `memory`, `listening` and `remote` in `ServiceInfo`, `system`, and `on_stats` calls |
| `control` | `start`, `stop`, `restart` work (in test mode they are recorded instead) |
| `policy` | `check_start` is asked before each start (in test mode a refusal is only recorded) |
| `ui` | `show` displays a panel |
| `read_files` | `list_files` and `read_file` on a service's folder, read-only (API 2) |
| `propose_changes` | `propose_change` opens a review window for the user (API 2) |
| `create_services` | `project_templates`, `create_project`, `list_projects` and `delete_project`: services from built-in templates (API 4); `leftover_projects` and `delete_leftovers` |

Official plugins get what they declare. Third-party plugins get only what the user grants, which can be less.
`self.permissions` holds what this run was granted, and `self.test_mode` whether it is in test mode.

## The Plugin class

```python
from pythonator.sdk import Plugin, ServiceEvent, ServiceInfo, SystemInfo, ui
```

The entry file must define exactly one subclass of `Plugin`.

### Attributes

| Attribute | Type | Holds |
| --- | --- | --- |
| `settings` | `dict[str, Any]` | The settings, defaults filled in |
| `services` | `dict[str, ServiceInfo]` | Every service by id, kept up to date |
| `system` | `SystemInfo` | The whole computer's CPU and memory use (`inspect`), refreshed before `on_stats` |
| `permissions` | `frozenset[str]` | Permissions granted for this run |
| `test_mode` | `bool` | Whether actions are only recorded |
| `data_dir` | `Path` | A private folder for the plugin's own files; the only place it can write |

### Methods to override

| Method | Called |
| --- | --- |
| `on_start()` | Once, after `settings` and `services` are filled in. Start background threads here |
| `on_stop()` | Before the plugin is stopped. Keep it short |
| `on_event(event)` | A service started, stopped, crashed... (see the kinds below) |
| `on_output(service, lines)` | New output of a service, up to 200 lines at a time (`read_logs`) |
| `on_stats()` | Every 2 seconds, after `services` got new CPU, memory and ports and `system` new figures (`inspect`) |
| `check_start(service) -> str \| None` | Before a start: return a reason to refuse it (`policy`); answer within 2 s |
| `on_settings()` | The user saved new settings |
| `on_action(action, value)` | A panel button, input, drop-down or tab was used |
| `on_tick()` | About every 5 seconds |
| `on_proposal(id, status, note)` | The user answered a proposed change: `applied`, `rejected`, `failed` or `expired` (API 2) |

An exception in any of them is reported in the plugin's Activity with its traceback, and the plugin keeps running.

### Methods to call

| Method | Does | Needs |
| --- | --- | --- |
| `log(text)` | A line in the plugin's Activity | |
| `notify(text, level="info")` | A notice in the window; `level` is `info`, `ok`, `warn` or `error` | |
| `show(panel)` | Shows or replaces the plugin's panel | `ui` |
| `start(id)`, `stop(id)`, `restart(id)` | Acts on a service | `control` |
| `list_files(id)` | `{"files": [{"path", "size"}], "hidden", "truncated", "entry"}` | `read_files` |
| `read_file(id, path)` | `{"path", "text", "truncated", "size"}` | `read_files` |
| `propose_change(id, path, edits, summary)` | Returns a proposal id; the answer comes as `on_proposal` | `propose_changes` |
| `project_templates()` | `{"templates": [{"key", "label", "tool", "port", "kind", "ready", "needs"}], "folder"}`: what can be made on this computer | `create_services` |
| `create_project(template, name, folder="", start=False)` | `{"id", "folder", "port"}`: the new service; it is made in its own sandbox, its progress shows in its console and as events | `create_services` |
| `list_projects()` | `[{"id", "name", "template", "folder", "state", "port"}]`: the services made from templates | `create_services` |
| `delete_project(id)` | Asks the user, in Pythonator's window, whether to delete that project and its folder; nothing is deleted unless they say so | `create_services` |
| `leftover_projects(folder="")` | `{"folder", "leftovers": [{"folder", "name", "made"}]}`: folders in the projects folder no service uses any more. In the default projects folder every unused folder; elsewhere only ones the Project builder made. Nothing inside them is read | `create_services` |
| `delete_leftovers(folders, folder="")` | Asks the user, in Pythonator's window, which of those folders to delete (one window for all of them); returns how many were asked about. Nothing is deleted unless they say so | `create_services` |

All of them are safe to call from any thread. The methods from `list_files` onwards block until Pythonator answers
(15 s at most) and raise `PluginError` with the reason when refused, so they should be called from a background
thread rather than from an `on_*` method.

### Files (API 2)

A plugin's sandbox never gets a service's folder. With `read_files` it asks Pythonator, which answers file by file:

* Only paths inside the service's folder, links included. `.git`, `node_modules`, virtual environments and build
  folders are skipped.
* Never secrets: `.env` files and the service's env file, private keys and certificates, credential, token and
  password files (`tokens.json`, `secrets.yaml`...), databases. `list_files` leaves them out and counts them in
  `hidden`; `read_file` refuses them.
* The values of the service's environment variables are replaced by `[NAME hidden]` in what is returned.
* Text only, 256 KB per file, at most 120 requests a minute.

### Proposed changes (API 2)

`propose_change` takes exact `(old, new)` replacements: each `old` must occur in the file exactly once (a single
edit with an empty `old` creates a new file of up to 48 KB). Pythonator applies them to the current file and opens a
review window with the diff and any warnings. **Nothing is written unless the user chooses Apply there.** Apply
refuses if the file has changed in the meantime, and keeps a copy of the previous file in the data folder's
`backups` folder.

Some changes are refused regardless of what the user would choose: scripts and executables (`.sh`, `.bat`, `.ps1`,
`.exe`, `Makefile`, …), CI configuration (`.github/`, `.gitlab-ci`), secret files, any path through a symbolic link,
emptying a file or removing most of it, edit or placeholder markers, and added code that matches patterns for running
programs or shell commands, deleting files, evaluating strings, calling the operating system directly (including
renamed imports, `getattr` with a constructed name and `__builtins__`) or adding an npm install script. These checks
are pattern-based, not a proof, so the diff in the review window is what matters. An edit that touches a hidden value
is refused with the same message as text that is absent or ambiguous, so a plugin cannot infer the value. Changes that
add web addresses, network use or dependencies are allowed with a warning. Each plugin may have one proposal pending
at a time; an unanswered proposal expires after 30 minutes. In test mode a proposal is only recorded.

### ServiceInfo

Read-only. The fields under *inspect* are zero or empty without that permission.

| Field | Type | |
| --- | --- | --- |
| `id`, `name` | `str` | |
| `runtime` | `str` | `python`, `node`, `go`... or `custom` |
| `mode` | `str` | `sandbox`, `container` or `full` |
| `state` | `str` | `stopped`, `starting`, `running`, `stopping`, `waiting`, `failed`, `task` |
| `network` | `bool` | May use the network |
| `ports` | `tuple[int, ...]` | The ports it declares |
| `expose` | `str` | `local` or `lan` |
| `memory_limit_mb` | `int` | 0 for none |
| `cpu_limit` | `int` | Percent of one core for the whole tree, 0 for none |
| `restarts` | `int` | Restarts in the last 10 minutes |
| `cpu` | `float` | *inspect*: percent of one core |
| `memory` | `int` | *inspect*: bytes, whole process tree |
| `listening` | `tuple[int, ...]` | *inspect*: ports it really listens on |
| `remote` | `tuple[str, ...]` | *inspect*: `address:port` it is connected to, up to 50 |

### SystemInfo

The whole computer, with *inspect*. Read-only.

| Field | Type | |
| --- | --- | --- |
| `cpu` | `float` | Percent of all cores together, 0 to 100 |
| `memory` | `float` | Percent of memory in use, 0 to 100 |
| `memory_total` | `int` | Bytes |
| `cores` | `int` | Logical cores: a service's `cpu` can go up to `cores * 100` |

### ServiceEvent

| Field | |
| --- | --- |
| `kind` | `started`, `stopped`, `exited`, `crashed`, `gave_up`, `blocked`, `installing`, `installed`, `install_failed` |
| `service` | The `ServiceInfo` after the event |
| `exit_code` | For `exited` and `crashed` |
| `note` | A short explanation, for example the reason of a `blocked` start |

## Panels

Panels are built with `ui`. They are plain data, validated by Pythonator and drawn with native Qt widgets; text is
always shown as text, never interpreted as HTML.

| Builder | Shows |
| --- | --- |
| `ui.panel(title, *items)` | The panel: a title (up to 80 characters) and up to 200 items |
| `ui.text(text, tone="")` | A paragraph, up to 4000 characters |
| `ui.stat(label, value, tone="")` | A small card with a label and a big value |
| `ui.table(columns, rows, tones=None)` | Up to 12 columns and 500 rows; `tones` colours each row |
| `ui.button(label, action, value="", icon="")` | A button that calls `on_action(action, value)`; `action` is letters, digits and `_.:-`. With `icon` (API 5) it is a small icon with `label` as its tooltip: `add`, `refresh`, `delete`, `play`, `stop`, `send`, `check`, `info`, `lock`, `settings`, `save`, `open`, `close`, `up`, `down`, `more`, `dots`, `log` or `package` |
| `ui.tabs(action, options, value)` | A switch between the panel's views, 2 to 6 names of up to 30 characters; choosing one calls `on_action(action, option)`. Put it first and send only that view's items (API 5) |
| `ui.row(*items)` | Items side by side, up to 20 |
| `ui.code(text)` | Monospaced, selectable text, up to 4000 characters (API 2) |
| `ui.input(action, placeholder="", button="Send")` | A text box: Enter or the button calls `on_action(action, text)`, Shift+Enter is a new line; up to 16,000 characters; what the user typed survives panel updates (API 2) |
| `ui.select(action, options, value="", label="")` | A drop-down of up to 300 options; choosing one calls `on_action(action, option)` (API 2) |
| `ui.chat(*messages)` | A chat view that fills the panel's height and scrolls on its own, following new messages while scrolled to the end; up to 200 messages (API 3) |
| `ui.message(role, text, code=False, markdown=False)` | One chat message: `role` is `you`, `ai`, `note` or `error`; up to 20,000 characters; messages of one role in a row share a bubble, `code=True` shows a monospaced block (API 3), and `markdown=True` formats an `ai` message as Markdown, with HTML off and nothing loaded (API 4) |

### Sizing (API 5)

Sizes are Pythonator's, never the plugin's: a panel can't set a width, height, font or colour, and every element is
made to fit the narrowest Plugins dock, 240 pixels.

- Text wraps to the panel's width, long words, paths and URLs included.
- A button is as wide as its label and never wider; when there's no room its label ends in "…", with the whole
  label in its tooltip. An icon button is always 30 × 28.
- A drop-down takes the rest of its row; its label goes above it when the row is narrow; long options end in "…".
- A row puts its items side by side and wraps onto more lines when they don't fit.
- A table scrolls sideways inside itself; a long cell ends in "…" with the whole text in its tooltip.
- The page is exactly as wide as the dock, so nothing can reach past its edge.

A panel therefore fits any width without extra work. Keep each view to one screenful and move further content into a
second view (`ui.tabs`) rather than a longer page. Icon buttons suit common actions (refresh, delete, stop); other
labels should be short.

A tone is `""`, `ok`, `warn`, `error` or `muted`. A select shows the `value` the plugin sends: after a pick it shows
that value until the next panel, so a placeholder option makes a one-shot menu. A panel holds at most 500 items in
all (each chat message counts). A panel that breaks these rules is refused, with the reason in the Activity, and the
previous panel stays.

## Limits

| Limit | Value | What happens |
| --- | --- | --- |
| Messages per second | 200, bursts of 400 | Extra messages are dropped; after 2000 dropped the plugin is disabled |
| Message size | 1 MB | The plugin is disabled |
| Not a protocol message | | The plugin is disabled |
| Crashes | 3 in 10 minutes | The plugin is disabled; before that it restarts after 2 s, then 4 s |
| `check_start` | 2 s | The start goes ahead without the plugin's answer |
| File and proposal requests | 120 a minute | Refused with a reason; the plugin keeps running |
| New projects (`create_project`) | 10 an hour | Refused with a reason |

## The protocol

The SDK implements the protocol, so plugins do not need to handle it directly. Each message is one JSON object per
line ([JSON Lines](https://jsonlines.org/)), UTF-8, with the message type in `t`.

**From Pythonator to the plugin** (on its stdin):

| `t` | Fields |
| --- | --- |
| `init` | `settings`, `permissions`, `test_mode`, `services` (a list of service views) |
| `event` | `kind`, `exit`, `note`, `service` |
| `services` | `services`: the list changed |
| `output` | `id`, `lines` |
| `stats` | `services` with CPU, memory and ports, and `system`: `cpu`, `memory`, `memory_total`, `cores` |
| `check` | `req`, `service`: answer with `verdict` |
| `settings` | `settings` |
| `action` | `action`, `value` |
| `reply` | `req`, `ok`, `data` or `error`: the answer to a `request` |
| `proposal` | `id`, `status`, `note`: the user answered a proposal |
| `tick` | |
| `stop` | |

**From the plugin to Pythonator** (on its stdout):

| `t` | Fields |
| --- | --- |
| `hello` | Sent once when loaded; Pythonator answers with `init` |
| `log`, `error` | `text` |
| `notify` | `text`, `level` |
| `panel` | `panel` |
| `control` | `op` (`start`, `stop`, `restart`), `id` |
| `verdict` | `req`, `deny` (empty to allow) |
| `request` | `req`, `op` (`list`, `read`, `propose`, `templates`, `create`, `projects`, `delete`, `leftovers`, `delete_leftovers`), and the fields that op needs (`id`, `path`, `edits`, `summary`, `template`, `name`, `folder`, `folders`) |

The worker sends the protocol on the real stdout and redirects `print()` to stderr, which becomes the Activity.

## Compatibility

The API version is raised only for changes that add capabilities plugins may depend on; existing plugins keep
working. New optional fields and methods are marked with the API version that introduced them. Pythonator refuses a
plugin that declares a newer `api` than its own, with a message saying that a newer Pythonator is needed.
