# Services

A service is a folder, a command and how to run it. Pythonator keeps what runs equal to what you asked for: the
background service checks every second and starts, stops or restarts to match.

## The fields

| Field | Meaning | Default |
| --- | --- | --- |
| `name` | lowercase letters, digits and `-` | from the folder |
| `folder` | the project folder, read-write; the service's working folder | |
| `command` | what to run, for example `main.py --port 8000` | |
| `runtime` | `python`, `node`, `bun`, `deno`, `go`, `java`, `dotnet`, `rust`, `ruby`, `powershell` or `binary` | `python` |
| `runtime_version` | a Python version installed with uv (Engines → Runtimes) | the one found |
| `mode` | `sandbox`, `container`, `wsl` or `full` | `sandbox` |
| `network` | traffic out (off, hosts, any) and ports in, see [Networking](networking.md) | no network |
| `limits` | `memory_mb`, `cpu` (cores), `tasks` (processes and threads) | 256 MB, 1, 128 |
| `restart` | `policy` (`on-failure`, `always`, `never`), back-off and give-up | on failure, 2 s, 5 tries |
| `health` | `tcp` or `http` check on a port, how often, and how many failures restart it | off |
| `schedule` | a cron line; runs it at those times instead of all the time | none |
| `env` | plain environment variables | none |
| `secrets` | names of secrets kept in your system keyring | none |
| `folders` | extra folders, each read-only or read-write | none |
| `container` | `image` and `engine` (podman, docker or auto), Container mode only | |
| `group` | a group name for the list, for example `web` | none |

Specs are stored as JSON in Pythonator's data folder (`services/<name>.json`, see
[Getting started](getting-started.md#1-install) for where that is). Each save bumps the *generation* and keeps the
previous file as `.bak`.

With a port open, the service gets `PORT` (the first port) and `HOST` (`127.0.0.1`, or `0.0.0.0` where it has its
own network or a port open to your local network) unless you set them yourself.

## Modes

**Sandbox** (recommended). A jail on this computer, built from what the OS offers:

* Linux: bubblewrap namespaces, Landlock, a seccomp filter and `no_new_privs`, and `pasta` for network.
* macOS: a Seatbelt profile written for this service, with the egress gate for hosts.
* Windows: an AppContainer with access granted to its folders only, inside a job object. Windows blocks ports in and the
  egress gate for an AppContainer, so a service that serves a port or needs Allowed hosts uses WSL or Container mode;
  New service suggests that when the Fit check finds a port.

The service sees its folder, its runtime and nothing else of your home. See [Isolation](isolation.md).

**Container.** Podman (rootless, preferred) or Docker, with `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, the limits, your user id, a network of its own, and labels so Pythonator only
ever touches its own containers. Each runtime has a default image; set your own with `--image`. Your folder is
mounted at `/app`.

**WSL** (Windows). The Linux sandbox, run inside Pythonator's own small Debian distribution with no Windows
drives and no interop. Set it up once from Engines → WSL → **Set up**.

**Full access.** Runs as you, with your files and network. The plan says *Files · not enforced* and
*Network · not enforced*, the console's first line says so, and Guard can restrict it (see
[Plugins](plugins.md#guard)).

## GUI apps

A service can be a desktop app that opens windows (PyQt, PySide, Tk, wx, Kivy, pygame, Electron). Turn on
**GUI app** on the first step of New service or in Settings → General; the Fit check turns it on when it finds
one of those imports. It is a widening, so the plan waits for your OK.

| | Sandbox | Container | WSL | Full access |
|---|---|---|---|---|
| Windows | Yes: its own windows only; other programs' windows, the clipboard and global hooks stay blocked by the job's UI limits | No | No | Yes |
| Linux | No | No | — | Yes, on your display |
| macOS | No | No | — | Yes |

On Linux and macOS a jailed program given your display could read other windows and what you type (X11), so
Pythonator refuses it there (GUI-001) rather than pretend it's contained. On Linux the background service passes
on `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY` and the session bus; if it was started without them, the plan says
so (GUI-LNX-002). Stop closes the app's windows with the rest of its process tree. Pythonator's own helper programs
never open a console window.

## Restarts

With `on-failure` (default), a clean exit (code 0) stops the service and says *finished*; a crash restarts it after
2 s, then 4 s, 8 s… up to 5 minutes. A run that lasts 30 s resets the count. After 5 crashes in a row it stops,
shows **Gave up** and asks you to look at the log. `always` restarts even after a clean exit; `never` never
restarts.

## Schedules

Give a service a cron line in Settings → Schedule (for example `0 3 * * *`, every day at 03:00) and it runs at
those times instead of all the time.

* **Start** turns the schedule on; **Stop** turns it off. Between runs it shows *next run 03:00*.
* A run that fails shows the exit code and still waits for the next time; it isn't restarted.
* If the computer was off at 03:00, it runs once when Pythonator next starts (`catch_up`, on by default). Set
  `"catch_up": false` in the spec to skip missed runs.

## Health checks

A health check asks the service whether it is working, not only whether it is running:

```json
"health": {"kind": "http", "port": 8000, "path": "/health", "every_s": 10, "fails": 3, "grace_s": 20}
```

`http` sends `GET path` to `127.0.0.1` and expects a status below 500; `tcp` expects the port to accept a connection. The first check
waits `grace_s` after a start. After `fails` failures in a row, the service is restarted and listed under
*Healed today* with the reason.

## Limits

| | Memory, CPU, tasks enforced by |
| --- | --- |
| Linux, Sandbox | a systemd user scope (cgroup v2); without a systemd user session, a watchdog for memory |
| Windows, Sandbox | the job object (memory, CPU rate, active processes) |
| macOS, Sandbox | a watchdog that checks every second; shown as *Limits · reduced* |
| Container | the engine (`--memory`, `--cpus`, `--pids-limit`) |
| WSL | the systemd scope inside Pythonator's distribution, when present; otherwise the watchdog |

A watchdog stops the service when it goes over memory; it doesn't cap CPU or tasks, and the plan says so.

## Groups

Give related services the same group (`--group web`) and the service list gets a filter for it. The command palette
(Ctrl+K) can restart every service in a group.

## Snapshots

When you approve a plan, when you keep changes from *Try in a copy*, and whenever you ask (Settings → Git),
Pythonator takes a snapshot of the service folder into a
private git repository in its data folder. It never uses or changes a `.git` in your folder, and it runs git with
hooks, links and background maintenance turned off. Settings → Git lists the snapshots, takes one, and restores
one (it writes the files back; links are refused).

## Logs

Output goes into the service's log: 4 MB segments, the last 16 kept (64 MB per service), with an index so the window
reads only the lines on screen. Stderr is shown in amber, Pythonator's own lines (start, stop, why) start with `›`.

Values of the service's secrets, secret-looking variables, passwords in URLs, known token formats and PEM private
keys are replaced with `••••` **before** the line is written, so they never reach the disk, a plugin or a webhook.

## Secrets

Secrets live in your system keyring (Credential Manager on Windows, the login Keychain on macOS, Secret Service on
Linux, for example GNOME Keyring or KWallet), under the service `pythonator` and the key `<service>/<name>`. The spec stores only the names.

Add, set and remove them in Settings → Variables → Secrets. The value is typed into a masked field and is never
written to a file. A service whose secret isn't set yet is **Blocked** and tells you where to set it. At start, each value is passed to the service as an environment
variable and added to the log masker. Values never appear on a command line, in a spec, or in the plan.

## When the world changes under it

The **Needs you** panel lists drift: a service that gave up, a spec that changed but hasn't been approved, a layer
that got weaker since you approved (for example bubblewrap was uninstalled, or a runtime moved). Nothing is fixed behind your back.
