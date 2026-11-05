# Changelog

Pythonator follows [semantic versioning](https://semver.org/). Each release lists what changed for users.

## 0.0.6

- **Start all** and sign-in start services one after another, top to bottom, by default. **Settings → Starting
  services** can start a few at a time or all at once instead.
- The service list itself orders services: select several with Ctrl or Shift, move them with the ↑ ↓ buttons or
  Alt+↑ / Alt+↓, remove them together with Delete or the right-click menu, which can also start or stop them all.
  The separate start-order window is gone.
- **Settings** has log rotation (size and how many old logs are kept, applied at once), keeping Pythonator in the
  tray when its window is closed, and starting it with Windows (or at sign-in on macOS and Linux), in the tray.
- Two services in one folder no longer make its `.venv` at the same time: one makes it, the other waits.

## 0.0.5

- **Start all** and login start services in a queue: in list order, a few at a time (**Settings → Start at once**,
  4 by default; 1 starts them one after another). The next one starts as soon as one before it is up.
- **Settings** also sets the start order (move services up and down) and removes several services at once.
- A first start no longer sets up pip when the project has nothing to install: the venv alone takes a second.

## 0.0.4

- Own environments are made and filled in Sandbox mode on every system: on Linux and macOS the setup steps can
  write the project's `.venv` (it was mounted read-only), and on Windows pip is set up as a step of its own.
- The console scrolls smoothly instead of in bursts: output is spread over every frame (60 a second, 30 under a
  flood), and the background service's "skipped" note comes at most once a second.
- Much less CPU under floods: the background service gathers a program's output in chunks instead of reading it
  a line at a time (84% of a core down to 7% in a flood test), and the console caps what one frame draws.

## 0.0.2

- **Own environment** per service, on by default: Start makes the project's `.venv` (or `node_modules`, `vendor`)
  when it's missing, Deno, Go, .NET and Rust get a private package cache, and nothing comes from global installs.
  The service dialog shows where its packages live, and **Packages ▾ → Recreate environment** starts it fresh.
- The editor stays responsive while a service floods its console: the console draws a few milliseconds a frame,
  pauses while you type or scroll, thins out floods smoothly (the log keeps everything) and handles progress bars
  (carriage returns) like a terminal. Scrolling big files never waits on syntax colouring.
- Plugin panels (the AI assistant) no longer take the keyboard: a redraw keeps your typing, cursor and focus.

## 0.0.1

The first release from the relaunched repository, with everything the preview builds had. Services and settings
from a preview build carry over.

- Runs scripts, web servers and bots and keeps them running: restarts with back-off, schedules (intervals or cron),
  memory and CPU limits for the whole process tree, live CPU, memory and ports, and rotating logs with secrets masked.
  Services keep running when the window closes.
- Sandbox mode by default, using the operating system's own isolation (Landlock, seccomp and bubblewrap on Linux,
  Seatbelt on macOS, AppContainer and a job object on Windows), Container mode with hardened Docker or Podman, and
  Full access only when you confirm it.
- Per-service ports, with a choice of this computer only or the network too, and **Make reachable** on Windows.
- Python, Node.js, Bun, Deno, Go, Rust, Java, .NET, Ruby, PHP, PowerShell, shell scripts or any command.
- Sandboxed plugins (plugin API 2): Webhooks, Guard, Health checks and the AI assistant are included.
- A built-in editor, Git clone and pull, and installers for Windows, macOS and Linux (x64 and arm64).

## Preview builds

Published before the relaunch, numbered 0.0.1 to 0.0.11. Their downloads are retired; the notes stay for reference.

### Preview 0.0.11

**CPU and memory limits per service**

- A **CPU limit** next to the memory limit (Configure → Restarts & limits), in percent of one core for the whole
  process tree, all threads and child processes together: 50 is half a core, 150 one and a half, and this
  computer's cores times 100 is no limit. It works the same on a 2-core laptop and a 64-thread workstation.
  - Windows: a hard cap on the service's job object, kept by Windows itself.
  - Containers: `--cpus` (and `--memory` as before), kept by Docker or Podman.
  - Linux and macOS, in Sandbox and Full access: Pythonator pauses the tree for short slices whenever it goes over,
    re-measured twice a second, so a busy loop is held to its limit and a program that is mostly idle is never
    paused. Stopping a limited service is still instant.
- The console says at each start which limits a service has and how this system keeps them.
- The header shows the CPU use against the limit, like the memory.

**Guard keeps the computer from being overloaded**

- New rules: when the whole computer stays over 95% CPU or 92% memory for 30 seconds, the service using the most
  gets Guard's action (warn by default, or stop or restart). Only a service that is a real part of the load (a
  quarter of all cores, or a tenth of the memory) is acted on, so a busy game or build elsewhere doesn't stop
  your services. Both thresholds can be changed or turned off.

**An AI assistant (plugin API 2)**

- The AI assistant plugin is back, rebuilt for the sandboxed plugins and bundled as an official plugin, off until
  you enable it in **Plugins ▾ → Manage plugins…**. It answers questions about one service at a time with
  Claude (the default), OpenAI, Gemini, DeepSeek, Ollama or any OpenAI-compatible server, from the service's output
  and files, and suggests fixes. It can't run commands, delete files or write anything itself: every suggested
  change opens a review window, and only your Apply writes it.
- Plugin API 2, for any plugin: a text box, drop-down and code block for panels; the `read_files` permission to read
  a service's files read-only (never `.env` files, keys or other secrets, with the service's environment values
  hidden); and `propose_changes` to suggest changes through the review window. Changes to scripts and programs,
  emptying files, and code that runs shell commands, deletes files or evaluates strings are refused outright.
- Plugin settings of kind `secret` (API keys, tokens) now live in the system keychain (Windows Credential Manager,
  macOS Keychain, Secret Service on Linux), not in `settings.json`. A secret saved before moves there the next time
  it is saved.

**Credits**

- **Help ▾ → About Pythonator** is back, with the credits (who made it and what it is built on), the support
  link and the versions to copy into a bug report. Help ▾ also opens the documentation again.

**For plugins**

- Plugins with the `inspect` permission now get the whole computer's CPU and memory use (`self.system`), and each
  service's CPU limit (`cpu_limit`).

### Preview 0.0.10

A rewrite of the engine with 0.0.9's window on top. The ideas are the same; the code is new, about 10,000 lines
instead of 24,000, typed throughout and checked with `mypy --strict`.

**The window**

- 0.0.9's design and feel: the toolbar with **Start all** and **Stop all**, the CPU and RAM meter, the service cards
  with their state dot and isolation shield, the header with the name, a live info line and the service's buttons
  (labels shrink to symbols in a narrow window), the editor over the console, and the file tree under the services.
- Nothing in the window waits. It opens at once and connects to the background service right after; Start all and
  Stop all return at once and the list follows. Press **Stop** (or Stop all) again to kill what is still stopping.
- Floods don't slow it down, as in 0.0.9: the console draws for at most 10 ms a frame and skips what it can't show
  in time (with a note; the log keeps everything), the window gets only the shown service's output, and a program
  that writes faster than it can be handled waits on its pipe instead of building a backlog, so stops stay instant.
- One editor zoom: Ctrl+, Ctrl-, Ctrl+= and Ctrl+wheel all change it, every open file follows, line numbers stay
  readable, and Ctrl+0 resets it. The size is kept when the window closes.
- Plugins' panels open from **Plugins ▾ → Show plugin panels**, so the editor and console keep the width.

**Running services**

- A background service runs your services and keeps them running when the window closes. The window only shows
  and asks. A crash of either one leaves the services alone, and leftovers of a crash are found and cleaned up.
- Restarts back off from 1 s to 60 s and give up after a set number in 10 minutes; schedules take intervals,
  `@daily` or cron lines; a memory limit covers the whole process tree.
- Install packages runs inside the service's own isolation: a `.venv` for Python, `node_modules`, `vendor` for Ruby
  and PHP. Rust and PowerShell join the languages.
- An environment check before each start says what will fail (runtime missing, no or broken venv, missing
  `node_modules`), and **System check** says how Pythonator itself runs and what this computer's sandbox enforces.
- Secrets from the environment are masked in the console, the logs and plugin messages.

**Isolation**

- Three modes: **Sandbox** (default), **Container** and **Full access**. If a protection can't be applied, the
  service doesn't start.
- Linux: Landlock (files, TCP ports, scopes) and seccomp in a small standard-library launcher, plus bubblewrap
  when available. macOS: Seatbelt. Windows: AppContainer with recorded, reversible folder grants, and a job object.
- Ports: list them, choose *this computer* or *other computers too*. Windows: **Make reachable** adds a loopback
  exemption, a loopback permit and firewall rules for that service only (127.0.0.1, and private networks when
  asked); **Undo** removes them. The permit is what lets the browser on the same PC open a sandboxed server: the
  exemption and firewall rule alone never got past Windows' own block of loopback connections into an
  AppContainer.
- Containers run with every capability dropped, `no-new-privileges`, a read-only image and a process limit, and can
  join a container network to reach a database.

**Plugins**

- Every plugin runs in its own sandboxed process and talks to Pythonator through a checked message protocol.
- Official plugins are recognised by SHA-256 from a reviewed catalog. Third-party plugins show a warning, start in
  **test mode** with no permissions, and are disabled when they flood, send garbage or crash repeatedly.
- Six permissions: `network`, `read_logs`, `inspect`, `control`, `policy`, `ui`. Settings forms and panels are
  declared, not coded.
- Three official plugins: **Webhooks** (Discord, Slack, Telegram, Teams, ntfy, any URL), **Guard** (rules and a
  watchdog) and **Health checks** (a live panel).

**Also**

- The editor is QScintilla: fast with big files, highlighting for every language, multiple carets.
- Git: hardened clone and fast-forward-only pull.
- **Licensing** and **Support** in the toolbar.
- The background service's API is documented, with a command-line client in `docs/examples/pyctl.py`.
- Documentation rewritten, on the website and in `docs/`.
- Installers as in 0.0.9: a Windows `setup.exe` (and portable `.zip`), a `.dmg` for Apple silicon and for Intel
  Macs, and a `.deb` for x64 and arm64 Linux (and a `.tar.gz`). An update stops the background service first;
  uninstalling on Windows also takes the sandboxes' entries off folder permissions.
- CI on Windows, macOS and Linux, which also builds each installer, installs it and starts the app; `pip-audit` on
  hash-pinned lock files; releases built from locked dependencies, tested the same way and attested.

**Removed**

- The AI assistant plugin, the plugin `pyproject.toml` packaging, the `bot.json` and other old-format migrations,
  the in-app API reference generator, and the code signing scripts (the downloads are unsigned). 0.0.9's `pythonator.json` is still imported once.

### Preview 0.0.9

- Windows sandbox: **Make reachable…** makes a sandboxed server reachable from this PC on its own address
  (`http://127.0.0.1:8765/`), with or without ports under Serves on. It adds a firewall rule for that sandbox that
  admits this PC only. Press it once more for services set up with 0.0.8 or earlier.
- Dependencies menu: the venv controls are always there for Python services. A line says where the service runs,
  then **Create venv**, **Recreate venv…** and a **Use .venv** on/off switch, greyed out where they don't apply.
- A venv you create is used from then on, even if the service was set to another Python.
- Other runtimes' Dependencies menu says where their packages go. The docs compare each runtime's equivalent of a
  venv.

### Preview 0.0.8

- Python services: Pythonator asks before running outside the project's venv (no venv, a venv the service doesn't
  use, or a broken one). You can use the venv, create or recreate it, or continue without. The choice can be
  remembered per service.
- After creating or recreating the venv, the start, install or command you asked for runs in it.
- `$` commands that install for your whole account (`npm -g`, `yarn global`, `gem install`, `composer global`,
  `pip --user`...) ask first, and offer the project-local command instead.

### Preview 0.0.7

- Windows sandbox: "working folder read only" is enforced again for a service that was set to "read & write"
  before. Pythonator now also blocks writes that another folder's permissions would allow.

### Preview 0.0.6

- Removed the command line added in 0.0.5. Pythonator is a desktop app; everything is in the window.
- Cleaned up the changelog.

### Preview 0.0.5

- Windows sandbox: temporary files now go to the sandbox's own temp folder instead of the service's folder.
- Dependencies are checked for known vulnerabilities on every change and every week.
- Dependabot now also keeps the app's and the official plugins' requirements up to date.

### Preview 0.0.4

- Windows sandbox: a first attempt at keeping temporary files out of the service's folder (finished in 0.0.5).

### Preview 0.0.3

- Windows sandbox: pip installs and Python's `tempfile` work in sandboxed services.
- Guard plugin: rewritten as a security policy with 19 rules for isolation, exposed ports, outgoing connections,
  container images and a resource watchdog. Each rule can warn, stop or restart, with per-service exceptions.
  See the Guard plugin's README.
- Webhooks plugin: alerts on Discord, Telegram, WhatsApp, Slack, Teams, ntfy, Pushover, Gotify or your own
  endpoint, batched per service and rate-limited. See the Webhooks plugin's README.
- Plugin SDK 2: plugins can inspect a service's setup and resource use, and have multi-line and tabbed settings.
  See [Writing a plugin](docs/plugin-development.md).
- Each service's outgoing connections are now recorded with its listening ports.

### Preview 0.0.2

- Windows sandbox: "Create venv" works, and the venv always has pip.
- "Install dependencies" adds pip first to a venv that has none.
- Windows sandbox: `$` commands run in cmd, because PowerShell can't run in the sandbox.
- Sandboxed services no longer inherit Pythonator's own DLL search path or internal variables.

### Preview 0.0.1

The first public release. Install it from the release page; development builds (0.1.x) don't offer it as an
update. Your services, settings and logs are kept.

- Services in Python, Node.js, Bun, Deno, Go, Java, .NET, Ruby, PHP, shell, PowerShell or any program.
  See the Runtimes page of that version's docs.
- Restarts with back-off and a crash-loop limit, schedules, memory and CPU limits, autostart and start at login.
- A background service keeps services running while the window is closed.
- A sandbox per service on Linux, macOS and Windows, and Docker / Podman containers. See [Security](docs/security.md).
- An editor with highlighting and completion, a fast console with colours, rotating logs and history.
- Clone from Git and pull before each start.
- Plugins: official ones are verified against a reviewed catalog; third-party ones always run sandboxed.
  Official plugins: webhooks, guard and the AI assistant (Claude, OpenAI, Gemini, DeepSeek, Ollama).
- A website with guides, a glossary, a dependency list and an API reference.
