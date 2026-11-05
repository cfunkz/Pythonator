# Architecture

Pythonator is about 8,700 lines of typed Python in three kinds of process. This page shows what runs where, and why.

## Processes

```text
 ┌──────────── the window (PyQt6) ────────────┐
 │ service list · console · editor · dialogs   │   only shows things and sends requests
 └──────────────────────┬──────────────────────┘
                        │ local socket, token, JSON lines
 ┌──────────────────────┴──────────────────────┐
 │ background service (pythonator --daemon)    │   keeps running when the window closes
 │ supervisor · restarts · schedules · logs    │
 │ plugin host · stats                         │
 └───────┬──────────────┬──────────────┬───────┘
         │              │              │  stdin/stdout pipes
   ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴──────┐
   │ sandbox   │  │ container │  │ plugin     │   each in the OS sandbox
   │ launcher  │  │ engine    │  │ worker     │   (or a container)
   │  └ service│  │  └ service│  │  └ plugin  │
   └───────────┘  └───────────┘  └────────────┘
```

**The window** is a PyQt6 application. It holds no state of its own beyond what the background service tells it,
and it never starts a service itself. Closing it, or it crashing, leaves the services alone.

**The background service** is a Qt event loop without widgets (`QCoreApplication`). It owns the services, their logs
and the plugins. The window starts it when it isn't running.

**Each service** starts through the sandbox launcher, the container engine, or directly (Full access). **Each
plugin** runs as `pythonator --plugin-worker` in the same kind of sandbox.

## Inside the background service

One thread runs the event loop and owns all state. Other threads only read pipes and hand what they read over:

| Thread | Does |
| --- | --- |
| Main | Handles requests, decides restarts and schedules, writes logs, sends events. Ticks every 50 ms |
| One reader per service | Reads the program's output in blocks and puts it on a queue |
| One per plugin, two pipes | Reads the plugin's messages (and its stderr) and puts them on the queue |
| Stats | Every 2 seconds, CPU and memory of every process tree; every 6 seconds, their ports |

The main thread drains the queue for at most 30 ms a tick and sends output in batches, so a program printing a
million lines a second can't starve the rest. Nothing waits for a plugin: messages to plugins are written to pipes,
and a policy check has a 2-second deadline.

## The sandbox launcher

[`launcher.py`](../pythonator/launcher.py) is the one file that runs *inside* the sandbox, before the service. It
gets a plan as JSON (folders, ports, network on or off), applies it to itself, and then `exec`s the service, which
inherits every restriction and can't remove them.

* **Linux:** applies Landlock (files, TCP ports, scoping), installs a seccomp filter through `libseccomp`, and runs
  under bubblewrap's namespaces when bubblewrap works.
* **Windows:** creates the AppContainer profile, grants the folders, and starts the program in it, suspended, inside
  a job object, then resumes it.
* **macOS:** the sandbox is a Seatbelt profile passed to `sandbox-exec`, so the launcher isn't needed.

It uses only the standard library (`ctypes` for the system calls) and runs with `python -I -S`, so nothing on the
service's path or in its folder can change it. If any step fails it exits with code 126 or 127 and the service is
reported as *the sandbox couldn't start it*.

## Files

| File | Written by | Format |
| --- | --- | --- |
| `services.json` | Background service | `ServiceFile`, pydantic, validated on load; a file with bad entries is kept as `.bad` |
| `settings.json` | Background service | `Settings` |
| `running.json` | Background service | Process id and start time of each running service, to clean up after a crash |
| `logs/*.log` | Background service | Rotating text logs, secrets masked |
| `daemon.token` | Background service | A fresh random token on each start |

Every write goes to a temporary file, is flushed to disk, and replaces the old file in one step, so a power cut
leaves the old version or the new one, never half of each.

## The code

```text
pythonator/
  __main__.py      entry point: window, --daemon, --plugin-worker, --sandbox-exec
  paths.py         data folders, how to start Pythonator's own helpers
  launcher.py      the sandbox launcher (standard library only)
  sdk.py           the plugin SDK (standard library only)
  core/            no Qt: models, validation, store, runtimes, git, schedules, logs, ANSI, environment checks
  isolation/       building the command for each mode: linux, macos, windows, container
  daemon/          the background service: supervisor, runner, server, protocol, client
  plugins/         manifest, trust (official hashes), host, worker
  ui/              the window
plugins/           the official plugins: guard, health, webhooks
tests/             pytest, run on Linux, macOS and Windows in CI
docs/              these pages; website/ is their design
```

`core` and `isolation` import no Qt, so they can be tested and reused without a display.

## Libraries

Pythonator uses well-maintained libraries rather than its own versions of them, and only a few:

| Library | For |
| --- | --- |
| [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) | The window, the event loop of the background service, the local socket |
| [QScintilla](https://www.riverbankcomputing.com/software/qscintilla/) | The editor: Scintilla's lexers, folding and speed with big files |
| [pydantic](https://docs.pydantic.dev/) | Every stored file, request and plugin message is a typed model, checked on the way in |
| [psutil](https://psutil.readthedocs.io/) | Process trees, CPU, memory, ports |
| [platformdirs](https://platformdirs.readthedocs.io/) | The right data folder on each system |
| [cronsim](https://github.com/cuu508/cronsim) | Cron schedules |

And the tools already on your computer: `git`, `docker` or `podman`, `bwrap`, `sandbox-exec`. Pythonator calls their
command lines instead of reimplementing them, with fixed argument lists.

Not used on purpose: Qt's JSON and CBOR classes (`QJsonDocument`, `QCborValue`), which had a parser vulnerability;
the linter refuses them. Python's `json` module parses everything.
