# Plugins

Plugins add features to Pythonator without being trusted with your computer. Each one:

* runs as its **own process in its own jail**, with no network, no home folder and its own code read-only;
* starts with **no permissions**; you grant each one by name, and can take any back at any time;
* has its grants tied to a **hash of its code**: when the code changes, the grants are gone until you look again;
* has **every call counted**, including the ones Pythonator refused, on the Plugins page.

A plugin can't read your files, reach the network or change a service. It can only ask Pythonator for the things
it has been granted, one call at a time.

## Guard

Guard ships with Pythonator. It checks every new or changed service against your rules **before** the change is
saved, and refuses it with a reason and a fix:

```text
error GUARD-003: Guard: tool: Full access is only allowed inside /home/you/scripts.
fix: Use Sandbox mode, or move the project into an allowed folder.
```

It also tells you in the status bar when a service gives up after repeated crashes.

Install and turn it on from **Plugins** (the puzzle piece on the left): pick Guard, **Install**, tick its
permissions (`policy.check`, `services.read`, `ui.notify`), **Turn on**.

Its rules are its **settings**, edited on the Plugins page:

| Setting | Default | Refuses |
| --- | --- | --- |
| `full_access_only_inside` | `["~/scripts"]` | Full access for a folder outside these |
| `lan_ports` | `false` | ports reachable from the local network |
| `port_range` | `[1024, 9999]` | ports outside the range |
| `max_memory_mb` | `4096` | memory limits above this |

Settings are stored beside the plugin, not in its code, so changing them keeps its grants.

**Guard fails closed.** If Guard is on and trusted with `policy.check` but has crashed or doesn't answer within
2 seconds, changes are refused (`GUARD-001`, `GUARD-002`) until you fix it or turn it off. A broken guard never
means an open door.

## Permissions

| Permission | Lets the plugin | Notes |
| --- | --- | --- |
| `services.read` | list services, their state, mode and ports; receive `service.state` events | |
| `logs.read` | read masked log lines | granted per service with `--only` |
| `ui.notify` | show a notification in the window | |
| `ui.views` | add a section to a service's inspector, drawn by Pythonator from plain data | no plugin code runs in the window |
| `ui.statusbar` | show one short status item in the status bar | plain text |
| `net.service` | probe a service's own open port (health) | this computer only |
| `projects.create` | offer starter projects in New service | you still approve the service |
| `policy.check` | see each change before it is saved and refuse it with a reason | fails closed |
| `net.connect` | ask Pythonator to send an HTTPS request to a granted host | see below |
| `editor.assist` | answer in the editor and propose exact-text edits | you apply each edit |

**`net.connect`** is the only way a plugin reaches a network: the plugin's jail has none. Pythonator makes the
request for it, and only to a host you granted from the list the plugin declares in `plugin.toml`. It allows
https on port 443 only, with no user or password in the URL. The name is resolved by Pythonator and every address
must be public (no cloud metadata, this computer or your network). Redirects aren't followed, the certificate is
checked, and request bodies pass the secret masker. At most 2 MB goes out and 4 MB comes back, within 120 s. A
header or the whole URL may be `{secret:NAME}` for a key the plugin declared; Pythonator fills it in from the OS
keychain on the way out, so the plugin never sees it.

Grants are ticked and unticked on the plugin's page; a scoped grant such as `logs.read` can be limited to some
services. Unticking is always safe, and the page shows the plugin's grants, calls, refusals and log.

Every plugin call is checked against the grants **on every call**. A refused call returns an error to the plugin
(its SDK raises `PermissionError`) and adds one to the *Not granted* column on the Plugins page.

## Safe mode

**Safe mode** (bottom of the Plugins list) stops every plugin and starts
none. Services keep running. Use it if a plugin misbehaves and you want to look before deciding.

## The plugin jail

| Layer | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Process tree | bubblewrap `--unshare-all`, `--die-with-parent`, new session | process group and a run marker | job object |
| Files | Landlock + seccomp: its code and the SDK read-only, `/usr`, a private `/tmp`; no home | Seatbelt profile with the same list | AppContainer with read grants on its code and the SDK |
| Network | none (its own empty namespace) | none (no network rules at all) | no network capability |
| Bytecode | `-X pycache_prefix` outside the plugin: `__pycache__` shipped in a plugin is never loaded | same | same |

On every OS: a watchdog stops it above 256 MB; more than 1 MB in one message, 200 messages a second, 2 MB a second
on stderr, or 2 MB waiting unread stops it (and a plugin that isn't reading is disabled). Stderr keeps 50 lines a
second in the plugin's log and counts the rest. Crashes restart it up to 3 times, then it stays stopped with the
reason.

No jail, no plugin: `PLG-LNX-001` (no bubblewrap), `PLG-LNX-002` (no Landlock), `PLG-MAC-001`, `PLG-WIN-001`.

## Install a plugin from a folder

**Plugins → Install…**. Pythonator copies the folder into its data
folder through a reader that refuses symlinks and special files, checks the size (at most 400 files, 8 MB), and
hashes the copy. It is installed **off, with no permissions**.

Official plugins are recognised by comparing the hash of the installed copy with the copy that ships inside
Pythonator. A modified Guard shows as *third-party*.

## Write a plugin

A plugin is a folder with `plugin.toml` and a Python file. It uses only the standard library and the SDK,
`pythonator_sdk`, which Pythonator puts on its path.

```toml
# plugin.toml
[plugin]
id = "crash-alert"                     # lowercase, digits and '-'
name = "Crash alert"
version = "0.1.0"
description = "Tells you when a service gives up."
entry = "main.py"
permissions = ["services.read", "ui.notify"]

[settings]                             # optional; editable on the Plugins page
min_restarts = 3
```

```python
# main.py
from pythonator_sdk import plugin


@plugin.on("service.state")  # needs services.read
def changed(event):
    if event["phase"] == "failed":
        plugin.notify(f"{event['name']} gave up: {event['message']}", level="warn")  # needs ui.notify


@plugin.hook("policy.check")  # needs policy.check
def review(change):
    if change["mode"] == "full":
        return {"allow": False, "reason": "No Full access here.", "fix": "Use Sandbox mode."}
    return {"allow": True}
```

Pythonator loads `main.py`, then runs the SDK's loop for you, so keep work at import time short.

### SDK

| Call | Does | Needs |
| --- | --- | --- |
| `@plugin.on("service.state")` | called with each state change | `services.read` |
| `@plugin.hook("policy.check")` | called with each change before it is saved; return `{"allow": bool, "reason", "fix"}` | `policy.check` |
| `plugin.call("services.list")` | list of services | `services.read` |
| `plugin.call("services.logs", service=..., start=-1, count=100)` | masked log lines | `logs.read` for that service |
| `plugin.notify(text, level="info")` | a notification (`info`, `warn`, `error`) | `ui.notify` |
| `plugin.view(service, rows)` | rows of `{"label", "value"}` in that service's inspector | `ui.views` |
| `plugin.settings` | your settings, as a dict | |

`print()` goes to the plugin's log (shown on the Plugins page), not to the protocol. The protocol is one JSON
object per line on stdin and stdout, so a plugin in another language is possible: see
`pythonator/resources/sdk/pythonator_sdk.py` for the messages.

### What a `policy.check` hook sees

The same fields that decide whether a change needs your approval, plus what this change widens:

```json
{"name": "web", "mode": "sandbox", "folder": "/home/you/sites/blog", "folders": [],
 "traffic": "any", "hosts": [], "lan": false, "ports": [[8000, "this-computer"]],
 "limits": [256, 1.0, 128], "secrets": [], "image": "",
 "widens": ["opens port 8000 to this computer"]}
```

## Bundled plugins

| Plugin | What it does | Permissions |
| --- | --- | --- |
| **Guard** | your rules, checked before any change is saved | `policy.check`, `services.read`, `ui.notify` |
| **Notify** | crash, give-up, block and health alerts in the window, and optionally to Slack, Discord, ntfy or Pushover; at most one crash alert per service every 10 minutes | `services.read`, `ui.notify`, `ui.statusbar`, `net.connect` |
| **AI assistant** | answers in the editor about the open file and masked logs; proposes exact-text edits you review and apply | `editor.assist`, `logs.read`, `net.connect` (api.anthropic.com) |
| **Builder** | starter projects (API, worker, web app, scheduled job) for New service | `projects.create` |

Webhook text is data: Notify escapes `&`, `<` and `>` for Slack and sends Discord `allowed_mentions: {"parse": []}`,
so a service's output can't ping a channel or hide a link.

A bundled plugin is **official** only while its files hash to the value in
`pythonator/resources/plugins/official.json`. That file is part of the release, and a test fails CI when a bundled
plugin changes without it. A copy edited in the install folder is just another plugin, with no grants.
