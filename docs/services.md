# Services

A service is one program Pythonator looks after. It is either a **file to run** in a language Pythonator knows
(`bot.py`, `server.js`, `main.go`, `app.jar`...) or a **custom command** (`uvicorn app:app --port 8000`,
`./my-binary --serve`), and it lives in a **folder**, its project.

Press **New service**, or select a service and press **Configure**. The tabs are below.

## General

| Field | What it does |
| --- | --- |
| Name | Shown in the list. Letters, digits, spaces and `. _ - ( )`, up to 64 characters |
| Folder | The project folder. The program starts in it, and in Sandbox mode it is the folder the program may use |
| Language | Python, Node.js, Bun, Deno, Go, Java, .NET, Ruby, PHP, Rust, Shell, PowerShell, or **Custom command** |
| File to run | Relative to the folder. Empty is allowed where the language has a default (`npm start`, `go run .`, `cargo run`) |
| Command | Custom command only: the whole command line, split like a shell would but never run by one |
| Arguments | Passed after the file |
| Schedule | Empty: keep it running. Otherwise when to run it (see [Schedules](#schedules)) |
| Start when Pythonator starts | Starts it whenever the background service starts |

**Detect from folder** looks for a known entry file (`main.py`, `app.py`, `bot.py`, `index.js`, `main.go`...) and
then for project files (`package.json`, `go.mod`, `Cargo.toml`, `Gemfile`...).

Pythonator never runs a shell. Arguments go to the program exactly as written, so quotes, `;`, `|` and `$( )` are
just characters. The checks refuse invisible characters (NUL, right-to-left overrides, zero-width spaces) that
could make a command look different from what runs.

## Restarts

| Setting | Meaning |
| --- | --- |
| Restart: When it crashes | Started again when it exits with an error (the default) |
| Restart: Always, also after a clean exit | Started again whenever it exits, also with exit code 0 |
| Restart: Never | It runs once |
| Give up after | How many restarts within 10 minutes before Pythonator stops trying. 0 is no limit |
| Time to stop | How long a stop waits for the program to exit politely before it is killed |

The wait before each restart doubles: 1 s, 2 s, 4 s, up to 60 s. A service that ran for more than a minute
starts counting again from zero. When it gives up, the service shows **failed** and plugins such as
[Notify](plugins.md#notify) sends one *gave up* message instead of one per crash.

**Stopping** sends Ctrl+C on Windows and SIGTERM to the whole process group on macOS and Linux, waits for
*Time to stop*, then kills the whole process tree. Press **Stop** (or **Stop all**) again to kill it at once
instead of waiting. On Windows every service is in a job object, so nothing it
started survives. Containers get `docker stop` / `podman stop`.

If Pythonator itself is killed or the computer loses power, the next start finds what was left running (by process
id *and* start time, so a reused id is never touched) and stops it.

## Schedules

A scheduled service runs, exits, and waits for its next time. Between runs it shows **waiting** and the time of the
next run.

| Write | Runs |
| --- | --- |
| `every 30s`, `every 15m`, `every 6h`, `every 1d` | At that interval (10 seconds at least) |
| `@hourly`, `@daily`, `@weekly`, `@monthly` | At the start of each hour, day, week, month |
| `0 3 * * *` | A cron line: here 03:00 every day. Five fields: minute, hour, day, month, weekday |
| `*/5 9-17 * * 1-5` | Every 5 minutes from 9 to 17, Monday to Friday |

Times are your computer's local time. A schedule takes the place of the restart policy.

## Memory and CPU limits

Both are on the **Restarts & limits** tab, both are optional, and both apply to the whole process tree: every
thread and every child process together. The console says at each start which limits a service has and how they
are kept.

The box at the top of the limits shows this computer's cores and memory. Type a limit, or press one of the buttons
next to it. Leave it empty (or press **No limit**) for none.

* Memory: `512M`, `2G`, `1.5 GB`, `800` (megabytes). At least 16 MB. More than this computer has is lowered to
  all of it.
* CPU: cores, like `0.5`, `1.5` or `2 cores`, or a percent of one core, like `150%`. At least 5%. All the cores
  or more means no limit.

What you type is checked as you type: the line under it says what it means (for example *2 GB, 13% of this
computer's memory*), or, in red, what to write instead. A service can't be saved with a limit that doesn't read.

**Memory limit**

* **Windows**: the job object's limit; Windows refuses the allocation that crosses it.
* **Containers**: `--memory`, enforced by the engine.
* **macOS and Linux**: a watchdog checks every 2 seconds and stops the service when the tree is over the limit.

**CPU limit**, in percent of one core, like the CPU figure on the service card: 50 is half a core, 150 one and a
half cores. A computer with 8 logical cores has 800 in all, and a limit of all of them is no limit. A program with
one thread can never use more than 100.

* **Windows**: a hard CPU cap on the job object, kept by the Windows scheduler.
* **Containers**: `--cpus`, kept by the engine.
* **macOS and Linux** (Sandbox and Full access): Pythonator measures the tree twice a second and, while it is over,
  lets it run for only part of every tenth of a second (it pauses it with `SIGSTOP` and resumes it with `SIGCONT`,
  like `cpulimit`). A service under its limit is never paused, and a stop resumes it first, so it can still shut
  down cleanly.

The CPU limit keeps one service from slowing the others and the computer down. To protect the computer as a whole,
turn on [Guard](plugins.md#guard): it watches the whole computer's CPU and memory and acts on the service behind
an overload.

## Environment

Variables come from, in order (later wins):

1. Your own environment, minus Pythonator's own variables and, in a sandbox, your desktop session's (the display,
   D-Bus and SSH agent sockets).
2. The service's **env file**, for example `.env` in the project: `KEY=value` lines, `#` comments, optional quotes
   and `export`.
3. The variables in the **Environment** table.

In the service window, **＋ Add variable** adds a row and **− Delete** (or the Delete key) removes the selected rows;
**Import from env file** copies the env file's names into the table, keeping the values you already have.

Pythonator adds `PYTHONUNBUFFERED=1` and `FORCE_COLOR=1`, so output shows as it is printed and in colour. With a
port listed it also sets `PORT` to the first port and, when only this computer may connect, `HOST=127.0.0.1`.

`LD_PRELOAD`, `LD_AUDIT` and the `DYLD_` variables that load code into every program are refused.

## Console and logs

The console shows output as it arrives, with colours, and a line at the bottom sends text to the program's input.
**Wrap** wraps long lines. Right-click a service for **Clear console**, **Open folder** and **Remove**.

Values of variables whose name looks secret (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `..._KEY`, `PRIVATE`,
`CREDENTIAL`), and at least 6 characters long, are replaced with `••••••` before output is shown, written to a log
or given to a plugin.

Each service has a log in `logs/` in your [data folder](getting-started.md#where-your-data-is-kept). It rotates at
10 MB and keeps 3 old files (both are settings). When a program prints faster than the window can draw, the
window shows the newest output and says so; the log still gets every line.

## Install packages

**Install packages** prepares the project for its language, inside the service's own isolation (a sandboxed
service installs in its sandbox, with the network on for the install):

| Language | Does |
| --- | --- |
| Python | Makes `.venv` in the project (or repairs a broken one), then `pip install -r requirements.txt` or `pip install -e .` |
| Node.js | `npm ci` with a `package-lock.json`, else `npm install`; `pnpm` or `yarn` when their lock file is there |
| Bun | `bun install` |
| Go | `go mod download` |
| .NET | `dotnet restore` |
| Ruby | `bundle install` into `vendor/bundle` |
| PHP | `composer install` into `vendor` |
| Rust | `cargo fetch` |

Python services then run with the project's `.venv` automatically. See [Languages](languages.md).

## Environment check

Before each start the console lists anything that will make the start fail or surprise you: the language isn't
installed, the file to run is missing, `package.json` without `node_modules`, a Python project without a venv, a
broken venv whose Python was removed. **System check** in the toolbar shows the same for Pythonator itself: which
Python runs it, whether it runs from a virtual environment, whether it runs as administrator (it shouldn't), and
what the sandbox enforces on this computer.
