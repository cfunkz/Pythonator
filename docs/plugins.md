# Plugins

Plugins add what a service runner doesn't need to have built in: alerts, rules, health checks, dashboards, links to
other tools. Pythonator is built for them. Each plugin runs as a separate program in a sandbox of its own and talks
to Pythonator through a small, checked set of messages, so a plugin can't crash Pythonator, freeze the window, or do
more than you allowed.

Press **Plugins ▾ → Manage plugins…** in the toolbar to turn them on and off, set them up and grant permissions.

## Official and third-party

| | Official | Third-party |
| --- | --- | --- |
| Where it comes from | The `plugins/` folder of Pythonator itself | A folder or Git repository you install |
| How it is recognised | Its files match the SHA-256 recorded in the reviewed catalog (`pythonator/plugins/official.json`) | Anything else, including an official plugin with one byte changed |
| Permissions | What its manifest declares | **None** until you grant them, one by one |
| Test mode | No | **Yes**, until you take it out: what it tries is recorded, not done |
| Sandbox | Yes | Yes |
| Badge | Official · verified | Third-party · sandboxed |

Enabling a third-party plugin shows a warning with its name, where it came from, its SHA-256 and what it asks for,
before anything runs.

## Permissions

| Permission | Lets the plugin |
| --- | --- |
| `network` | Use the internet and your local network. Without it the plugin's sandbox has no network at all |
| `read_logs` | Receive the output of your services (with secrets already masked) |
| `inspect` | See ports, connections, memory and CPU of your services |
| `control` | Start, stop and restart services |
| `policy` | Refuse a service's start, with a reason shown to you |
| `ui` | Show a panel in the Plugins dock of the window (**Plugins ▾ → Show plugin panels**) |
| `read_files` | Read a service's files through Pythonator, read-only: never `.env` files, keys, certificates or other secrets |
| `propose_changes` | Suggest a change to a service's file. It opens a review window; only your **Apply** writes it |

Every plugin, whatever it is granted, can see the list of services (name, language, mode, state, ports) and write to
its own activity log. None can read your own files, Pythonator's data, or other plugins' data, and without
`read_files` none can read the services' files either.

## Secrets in settings

A plugin setting of kind `secret` (an API key, a token) is kept in your system keychain: Windows Credential
Manager, the macOS Keychain, or the Secret Service (GNOME Keyring, KWallet) on Linux. `settings.json` only notes
that it is there, and the window only ever shows `••••••`. Where there's no keychain (a Linux server without a
desktop), it goes in `secrets.json` in Pythonator's data folder, readable by you only, and the plugin's settings say
so. Removing a plugin deletes its secrets.

## The AI assistant

An official plugin, off until you tick **Enabled**. It answers questions about one service at a time with Claude,
OpenAI, Gemini, DeepSeek, Ollama or any OpenAI-compatible server, using the service's output and files, and
suggests fixes you review and apply. Your API key goes in the system keychain. See its
[README](../plugins/pythonator-ai/README.md).

## Test mode

A third-party plugin starts in test mode. In test mode:

* the permissions you grant still decide what it receives;
* a start, stop or restart it asks for is written to its **Activity** as *Test mode: would stop api* instead;
* a start it would refuse is recorded as *would block a start*, and the start goes ahead.

Watch the Activity for a while. When it does what you expect, untick **Test mode** and press **Apply**. The plugin
restarts with its new permissions.

## When a plugin misbehaves

Pythonator stops a plugin, disables it and says why when it:

* sends more than 200 messages a second for too long;
* sends one message larger than 64 KB, or anything that isn't a protocol message;
* crashes three times in ten minutes.

A plugin that doesn't answer a start check within 2 seconds is skipped for that start. Panels are checked against a
fixed set of elements (text, numbers, tables, buttons, rows), at most 500, shown as plain text: a plugin can't put
HTML, links or scripts into the window.

## Installing a third-party plugin

In the Plugins window, press **Install from folder…** (a folder with a `plugin.toml`) or **Install from Git…** (an
`https` address). The plugin is copied into your data folder and stays off until you enable it. **Remove plugin**
deletes it and its data. Official plugins can be turned off, not removed.

## The official plugins

### Webhooks

Sends a message when a service crashes, gives up restarting, runs again, or (if you want) starts and stops.

| Setting | Meaning |
| --- | --- |
| Webhook URLs | One per line. Formats itself for Discord, Slack, Telegram (`https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>`), Microsoft Teams workflows and ntfy; any other `https` address gets JSON `{"title", "text", "source"}` |
| When a service crashes / gives up / runs again / starts or stops | Which events to send |
| Last output lines in a crash alert | How much of the output goes with a crash |
| Seconds between alerts for one service | So a crash loop is a few messages, not hundreds |

Permissions: `network`, `read_logs`. Discord messages can't mention `@everyone`, whatever a service prints. Only
`https` addresses are used (and `http` to this computer). A service that answers 429 *Too many requests* is
retried after the time it asks for.

### Guard

Security rules for every service, checked before each start and watched while they run.

| Setting | Meaning |
| --- | --- |
| Don't start services in Full access mode | Refuses those starts |
| Don't let other computers reach any service | Refuses services exposed to other computers |
| Allowed ports | For example `3000-3999, 8000-8100, 443`; empty allows any |
| Act when a service listens on a port it didn't declare | Watches the ports each service really listens on |
| Memory limit / CPU limit for any service | A watchdog over every service |
| Computer overloaded: CPU over / memory in use over | When the whole computer stays over this for 30 s (95% CPU and 92% memory by default, 0 turns it off), the service using the most gets the action, if it is at least a quarter of the CPU or a tenth of the memory |
| When a running service breaks a rule | `warn`, `stop` or `restart` |

Permissions: `policy`, `inspect`, `control`, `ui`. Its panel lists violations with the time, the service, the rule
and what it did.

### Health checks

Calls each running service that serves a port, `GET http://127.0.0.1:<port><path>`, every few seconds, and shows a
panel of which ones answer and how fast. It can notify you when a check starts failing.

Permissions: `network`, `inspect`, `ui`. It only connects to this computer.

## Writing your own

A plugin is a folder with a `plugin.toml` and a Python file. [Writing a plugin](plugin-development.md) builds one
step by step, and [Plugin API](plugin-api.md) lists everything a plugin can do.
