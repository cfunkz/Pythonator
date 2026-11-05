# Background service API

The window is only a view. The work is done by Pythonator's **background service** (the daemon), which keeps
running when the window closes. The window talks to it over a local socket, and so can your own scripts, a
status-bar widget, a deploy script or a test harness. Anything the window can do, a script can do.

The socket isn't on the network: no other computer and no other user of this computer can open it.

## Connecting

| System | Address |
| --- | --- |
| macOS, Linux | The Unix socket `daemon.sock` in the [data folder](getting-started.md#where-your-data-is-kept), mode 0600. If that path is longer than 100 bytes (a deep `PYTHONATOR_HOME`), a socket can't live there, and it is `<16 hex>.sock` in `$XDG_RUNTIME_DIR/pythonator/` or else in `<temp folder>/pythonator-<your uid>/`, a folder only you can open; the hex is the start of the SHA-256 of the data folder's full path. `socket_path()` in `pyctl.py` does this |
| Windows | The named pipe `\\.\pipe\pythonator-<16 hex>`, where the hex is the start of the SHA-256 of the data folder's full path in lower case; only your account may open it |

The background service writes a fresh random token to `daemon.token` in the data folder (readable only by you)
each time it starts. Send it first:

```json
{"op": "hello", "req": 1, "token": "<the contents of daemon.token>"}
```

A wrong token, or any other message first, and the connection is closed. After a correct `hello` you receive a
reply, then a `snapshot` of every service and its state, the list of plugins, and their panels.

If the background service isn't running, start it with `pythonator --daemon` (the window does this for you).

## Messages

Both directions send one JSON object per line, UTF-8, at most 1 MB per line.

**Requests** have an `op` and a `req` number of your choice. Every request gets exactly one reply with the same
`req`:

```json
{"ev": "reply", "req": 7, "ok": true, "data": ...}
{"ev": "reply", "req": 8, "ok": false, "error": "Invalid id: String should match pattern ..."}
```

A request with a field it doesn't know, a missing field or a bad value is refused with an error, never guessed at.

**Events** arrive whenever something happens, to every connected client. They have an `ev` and no `req`.

## Requests

| `op` | Fields | Does, and `data` in the reply |
| --- | --- | --- |
| `hello` | `token` | Authenticates; `data`: `{"version", "protocol"}` |
| `ping` | | `"pong"` |
| `snapshot` | | Every service, its state and the settings, as in the `snapshot` event |
| `capabilities` | | `{"sandbox": [[protection, status]...], "app": [[level, text]...]}`, what System check shows |
| `add` | `service` | Adds a service (see [the service object](#the-service-object)) |
| `update` | `service` | Replaces the service with the same `id` |
| `remove` | `id` | Stops and removes it, with its logs and sandbox folders (not its project) |
| `start` | `id`, `confirmed` | Starts it. A Full access service replies `"error": "confirm_full"` until sent with `"confirmed": true` once |
| `stop`, `restart` | `id` | |
| `start_all`, `stop_all` | | |
| `input` | `id`, `text` | Sends a line to the program's input |
| `install` | `id` | Install packages |
| `pull` | `id` | git pull, fast-forward only |
| `tail` | `id` | The recent output, as one string |
| `clear` | `id` | Clears the recent output |
| `watch` | `id` | From now on, send this connection the `output` events of that service only (`""`: none). The window uses it for the service it shows |
| `check` | `id` | The environment check: `[[level, text]...]` |
| `settings` | `settings` | Saves the settings (plugin permissions change only through the plugin requests) |
| `plugin_enable` | `plugin`, `enabled` | |
| `plugin_settings` | `plugin`, `values` | |
| `plugin_grant` | `plugin`, `permissions`, `live` | Grants permissions to a third-party plugin; `live: true` leaves test mode |
| `plugin_action` | `plugin`, `action`, `value` | As if a panel button was pressed |
| `plugin_install` | `source` | A folder or a git address; `data`: the plugin's id |
| `plugin_remove` | `plugin` | |
| `shutdown` | `stop_services` | Ends the background service, stopping every service unless `false` |

## Events

| `ev` | Fields | When |
| --- | --- | --- |
| `snapshot` | `services`, `states`, `settings` | After `hello` |
| `services` | `services` | A service was added, changed or removed |
| `state` | `id`, `state`, `kind`, `pid`, `exit`, `note`, `restarts`, `started`, `task`, `next_run` | A service changed state. `kind` is the event: `started`, `stopped`, `exited`, `crashed`, `gave_up`, `blocked`, `installing`, `installed`, `install_failed` |
| `output` | `id`, `text` | New output, secrets masked, ANSI colour codes included |
| `stats` | `items`: `{id: {cpu, memory, processes, ports, remote}}`, and `system`: `{cpu, memory, memory_total, cores}` for the whole computer | Every 2 seconds, for running services |
| `notice` | `level`, `text` | Something to tell the user |
| `plugins` | `plugins` | A plugin was enabled, changed, crashed... |
| `plugin_activity` | `plugin`, `text` | A line in a plugin's Activity |
| `panel` | `plugin`, `name`, `panel` | A plugin's panel changed; `panel: null` removes it |

A client that reads too slowly first gets only the newest output (with a notice saying so), and is disconnected if
it falls 64 MB behind. The services don't wait for anyone.

## The service object

The same object is stored in `services.json`. Only `name` and `folder` are required.

```json
{
  "id": "3f9a1c2b7d10",
  "name": "web api",
  "folder": "/home/me/code/api",
  "runtime": "python",
  "entry": "main.py",
  "command": [],
  "args": ["--workers", "2"],
  "env": {"LOG_LEVEL": "info"},
  "env_file": ".env",
  "mode": "sandbox",
  "network": {"enabled": true, "ports": [8000], "expose": "local"},
  "sandbox": {"write_project": true, "folders": [{"path": "/home/me/datasets", "write": false}]},
  "container": {"engine": "auto", "image": "", "network": "", "read_only_root": true},
  "restart": "on-failure",
  "max_restarts": 5,
  "stop_timeout": 10.0,
  "autostart": false,
  "memory_mb": 0,
  "cpu_percent": 0,
  "git": {"url": "https://github.com/me/api.git", "branch": "", "pull_on_start": false},
  "schedule": ""
}
```

| Field | Values |
| --- | --- |
| `runtime` | `python`, `node`, `bun`, `deno`, `go`, `java`, `dotnet`, `ruby`, `php`, `rust`, `shell`, `powershell`, or `custom` with `command` |
| `mode` | `sandbox`, `container`, `full` |
| `network.expose` | `local` (this computer) or `lan` (other computers too) |
| `restart` | `never`, `on-failure`, `always` |
| `container.engine` | `auto`, `docker`, `podman` |
| `schedule` | empty, `every 15m`, `@daily`, or a cron line |
| `memory_mb`, `cpu_percent` | 0 for no limit; `cpu_percent` is percent of one core for the whole tree (150: one and a half cores) |

Every field is checked the same way as in the window: see [Services](services.md).

## Example: a command-line client

[`pyctl.py`](examples/pyctl.py) is a complete client in under 90 lines of standard-library Python, for Windows, macOS and
Linux:

```bash
python docs/examples/pyctl.py list
python docs/examples/pyctl.py start "web api"
python docs/examples/pyctl.py tail "web api"
```

The core of it:

```python
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(socket_path())  # daemon.sock in the data folder, see Connecting
file = sock.makefile("rwb")
token = (home / "daemon.token").read_text().strip()
file.write(json.dumps({"op": "hello", "req": 1, "token": token}).encode() + b"\n")
file.flush()
for line in file:
    message = json.loads(line)
    ...
```

## Versioning

`hello` returns the `protocol` number, 1 in this version. It changes only when a change would break existing
clients; new requests, events and fields don't change it.
