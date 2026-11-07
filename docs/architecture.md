# Architecture

Pythonator is about 19,000 lines of type-checked Python (`mypy --strict`), running as three kinds of process. This
page describes what runs where, and why.

## Processes

```text
 ┌──────────── the window (PyQt6) ────────────┐
 │ service list · console · editor · dialogs   │   displays state and sends requests
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

**The window** is a [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) application. It holds no state beyond
what the background service reports, and never starts a service itself. Closing it, or a crash of it, does not affect
the services. Only one window runs per data folder; starting Pythonator again brings the existing window to the
front ([`instance.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/instance.py)).

**The background service** is a Qt event loop without widgets
([`QCoreApplication`](https://doc.qt.io/qt-6/qcoreapplication.html)). It owns the services, their logs and the
plugins, and the window starts it if it is not already running. It exposes the
[background service API](service-api.md).

**Each service** starts through the sandbox launcher, the container engine, or directly (Full access). **Each
plugin** runs as `pythonator --plugin-worker` in the same kind of sandbox.

## Inside the background service

One thread runs the event loop and owns all state. Other threads only perform blocking I/O and hand results to it
through a queue:

| Thread | Role |
| --- | --- |
| Main | Handles requests, decides restarts and schedules, writes logs, sends events. Ticks every 50 ms |
| One reader per service | Reads the program's output in blocks and queues it |
| One writer per service | Writes typed input to the program; more than 256 unread lines are refused |
| Three per plugin | One writes messages to the plugin (a queue of up to 2,000), one reads its messages, one reads its stderr |
| Stats | Every 2 seconds, one process-table pass for CPU and memory of all process trees; every third pass, one connections call for their ports |
| Short-lived workers | Environment set-up, container preparation, git pulls, removals and folder deletion, so none of them block the main thread |

The main thread drains the queue for at most 30 ms per tick and sends output in batches, so a program printing
millions of lines a second cannot starve the rest. Nothing waits on a plugin: messages to plugins go through their
writer threads, and a policy check has a 2-second deadline.

## The sandbox launcher

[`launcher.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/launcher.py) is the only file that runs
*inside* the sandbox, before the service. It receives a plan as JSON (folders, ports, network on or off), applies it
to its own process, and then `exec`s the service, which inherits every restriction and cannot lift them.

* **Linux:** applies [Landlock](https://docs.kernel.org/userspace-api/landlock.html) (files, TCP ports, scoping),
  installs a [seccomp](https://man7.org/linux/man-pages/man2/seccomp.2.html) filter through `libseccomp`, and runs
  under [bubblewrap](https://github.com/containers/bubblewrap)'s namespaces when bubblewrap is usable. It is also a
  child subreaper ([`PR_SET_CHILD_SUBREAPER`](https://man7.org/linux/man-pages/man2/prctl.2.html)), so processes that
  leave the service's process group remain in its tree and are stopped with it.
* **Windows:** creates the AppContainer profile, grants the folders, and starts the program in it, suspended, inside a
  job object, then resumes it. A process that cannot be added to the job is never resumed.
* **macOS:** the sandbox is a Seatbelt profile passed to `sandbox-exec`, so the launcher is not needed.

It uses only the standard library (`ctypes` for system calls) and runs with `python -I -S`, so nothing on the
service's path or in its folder can alter it. If any step fails it exits with code 126 or 127, and the service is
reported as *the sandbox couldn't start it*.

## Files

| File | Written by | Format |
| --- | --- | --- |
| `services.json` | Background service | `ServiceFile` (pydantic), validated on load; a file with invalid entries is kept as `.bad` |
| `settings.json` | Background service | `Settings` (pydantic) |
| `running.json` | Background service | Process ID and start time of each running service, to clean up after a crash; rewritten only when the set of running services changes |
| `logs/*.log` | Background service | Rotating text logs, secrets masked |
| `daemon.token` | Background service | A new random token on each start, written once the socket is listening |
| `secrets.json` | Background service | Plugin secrets, only where no system keychain exists |

Every write goes to a temporary file, is flushed to disk, and then replaces the old file atomically, so a power cut
leaves either the old version or the new one, never a mixture.

## Source layout

```text
pythonator/
  __main__.py      entry point: window, --daemon, --plugin-worker, --sandbox-exec
  instance.py      one window and one background service per data folder
  paths.py         data folders, how to start Pythonator's own helpers
  launcher.py      the sandbox launcher (standard library only)
  sdk.py           the plugin SDK (standard library only)
  core/            no Qt: models, validation, store, runtimes, git, schedules, logs, ANSI, environment checks
  isolation/       the command for each mode: linux, macos, windows, container, compose; leftovers
  daemon/          the background service: supervisor, runner, server, protocol, client
  plugins/         manifest, trust (official hashes), host, worker
  ui/              the window
plugins/           the official plugins: guard, notify, builder, pythonator-ai
tests/             pytest, run on Linux, macOS and Windows in CI
docs/              these pages; website/ holds the site design
```

`core` and `isolation` import nothing from Qt, so they can be tested and reused without a display.

## Dependencies

Pythonator relies on a small number of well-maintained libraries rather than reimplementing them:

| Library | Used for |
| --- | --- |
| [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) | The window, the background service's event loop, the local socket |
| [QScintilla](https://www.riverbankcomputing.com/software/qscintilla/) | The editor: Scintilla's lexers, folding and performance on large files |
| [pydantic](https://docs.pydantic.dev/) | Every stored file, request and plugin message is a typed model, validated on input |
| [psutil](https://psutil.readthedocs.io/) | Process trees, CPU, memory and ports |
| [platformdirs](https://platformdirs.readthedocs.io/) | The conventional data folder on each system |
| [cronsim](https://github.com/cuu508/cronsim) | Cron schedules |

It also calls tools already on the computer (`git`, `docker` or `podman`, `bwrap`, `sandbox-exec`) through their
command-line interfaces, always with fixed argument lists.

Qt's JSON and CBOR classes (`QJsonDocument`, `QCborValue`) are deliberately unused because of a past parser
vulnerability; a lint rule in `pyproject.toml` rejects them, and Python's `json` module handles all parsing.
