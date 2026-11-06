# Changelog

Pythonator follows [semantic versioning](https://semver.org/). Each release lists what changed for users.

## 0.0.15

- The plugin dock stays smooth while you resize or move it, even with a long AI chat or a busy Notify panel. A panel
  is laid out again once the dock rests, not at every step of the drag (97 ms to 5 ms per step with a long chat),
  each label remembers its height for a width, and a plugin's updates are drawn at most every 60 ms, a hidden tab's
  only when it is shown (a streamed AI answer: 91 ms to 13 ms per piece).
- The Project builder (1.3) has 38 templates and a **Runtime** filter. New: React + TypeScript, Svelte, Solid,
  Preact and Vanilla JS on Vite; Next.js, Angular and Astro; Fastify, Hono and Koa APIs; Litestar, Streamlit and
  Gradio; Go (net/http and Gin), Rust (Axum), Bun (Hono and Elysia), Deno, Java, PHP and Ruby (Sinatra); a Node.js
  worker. Each one was made and started in the Linux sandbox and answered over HTTP.
- Runtime versions are checked before anything runs, for every runtime. Each template says which versions its
  framework supports, and a project's own files are read too (`package.json` engines, `requires-python`, `go.mod`,
  `rust-version`, composer's `php`). A Node.js that is too old is named with what to install, for example *Nuxt
  needs Node.js 22.22.3 or newer (this computer has 22.13.0)*. That was why Nuxt failed: Nuxt 4 and its packages
  need a newer Node.js 22 than 22.13, not the sandbox.
- A start notes a listed port that is already in use (and by which program) or, on Windows, reserved by Windows
  for Hyper-V, WSL or Docker. A service that runs but still doesn't listen on its port 30 seconds in gets a note
  with what its processes are doing, for every runtime.
- The console shows the command that really runs: in the Windows sandbox, the private copy of Node.js (since
  0.0.13), not `C:\Program Files\nodejs`.
- Linux sandbox: a service with listed ports may also listen on a port the system picks (port 0). Next.js's
  Turbopack and Nuxt talk to their own workers that way and failed without it; `listen()` without `bind()` got
  such a port already. Rust installed with rustup works in the sandbox (its toolchains folder is readable, and
  `RUSTUP_HOME` points at it).
- Nuxt runs as one process (`--no-fork`), Astro's dev server stays in the foreground, and Astro's starter is
  written by Pythonator instead of being downloaded from GitHub.
- Windows sandbox: Vite apps (React, Vue, Svelte and the others) start. Node.js hung, using a whole core and
  printing nothing, as soon as an app started another program: Node's pipe names are ones Windows refuses in a
  sandbox, and Node tries the next name forever. Vite starts one at once (`net use`); Next.js, Angular and npm
  install scripts do too. Sandboxes now run Pythonator's private copy of Node.js, with that one name moved under
  `LOCAL\`, as Node's next libuv does in a sandbox. See "Windows: Node.js programs that start other programs" in
  the isolation docs. Then Vite stopped at *EPERM: operation not permitted, realpath*: Windows refuses its own
  realpath in a sandbox too, so Pythonator's Node.js preload answers with the path itself when it exists. CI starts
  a Vite app in the Windows sandbox on every push.

## 0.0.14

- Windows: sandboxed npm and Node.js get past *EPERM: operation not permitted, lstat 'C:\'* (npm create vue,
  create-vite and the other Node templates). A sandbox may not look at the folders above its own, and Node.js
  looks at each of them on the way to a file; Node now starts in the sandbox without that walk, and a small preload
  answers for those folders. Full access was never affected. See "Windows: Node.js and the folders above the
  sandbox's own" in the isolation docs.
- Removing or deleting a project no longer holds anything up. Taking a sandbox's permissions back (file by file
  over a big node_modules on Windows, which took minutes) runs in the background, and a folder that is about to be
  deleted is skipped. The window and the other services keep running meanwhile.
- Deleting a project really deletes its folder on Windows: the window closes that project's open files and stops
  watching the folder first, paths deeper than 260 characters are reached, and a file a virus scanner holds for a
  moment is tried again.
- A first sandboxed Node start copies Node.js for sandboxes in the background too, with a note in the console.
- Project builder 1.2.1: one Create button. An empty name takes the suggested one, shown in the box.
- Windows CI: the check behind "Let sandboxes use it" is tested the way it works since 0.0.13.

## 0.0.13

- Windows: sandboxed Node.js projects (React, Vue, Nuxt, Express, the bots…) work with Node.js installed for all
  users in C:\Program Files\nodejs, with no administrator step. That folder doesn't let sandboxes in, and 0.0.12's
  check for it never fired (Windows reports every folder as writable to the test it used), so the start failed
  with "Access is denied". Sandboxes now run a private copy of Node.js that Pythonator makes once per version in
  its own data folder, and npm runs without a batch file in between.
- The AI assistant's changes land: when a model writes the prompt's example path (path/to/file.py) or the wrong
  folder, the edit goes to the one file that holds the text it replaces, and a line that differs only in trailing
  spaces still matches. An edit that matches no file is left out with a note, never created as a new file. The
  prompt asks for the file's real path.
- Plugin panels can't reach past the dock any more, whatever a plugin sends (plugin API 5): text and paths wrap,
  buttons end in "…" when there's no room, drop-downs take the rest of the row, and the page is exactly as wide as
  the dock, which is never narrower than 240 px. Plugins get views (`ui.tabs`) and icon buttons
  (`ui.button(..., icon="refresh")`). See "Sizing" in the plugin API.
- Notify 1.1 has two views, **Services** (figures and each service's state) and **Events** (the timeline, filters and
  alerts). The Project builder (1.2) has **New project** and **Projects**; the AI assistant is 1.3.
- The header is one line: the service's name, state and mode, with Start, Stop, Restart, Packages, Configure,
  **Details (i)** and More as icons (their words in the tooltips). Details lists the process, CPU, memory, ports,
  isolation, restart policy and folder; choosing a line copies it. It is also in the list's right-click menu.
- More room for code and output: the editor has no bar of its own (New, Open and Save sit beside its tabs), its
  tabs are lower, and the console's buttons are icons.

## 0.0.12

- Plugin panels are tabs in one Plugins dock again, as in 0.0.10: no separate windows, no lock. The console redraws
  smoothly while you resize or move the window, as before 0.0.11.
- Panels fit their dock: a row of cards, lists or buttons (Notify's figures and filters) wraps onto the next line
  instead of being cut off at the edge, and a wide table scrolls sideways.
- AI answers fit their bubble again, Markdown included: no more last lines cut off, and the note under your
  question no longer overlaps it.
- Windows: Node.js installed for all users (C:\Program Files\nodejs) without the read entry Windows gives
  C:\Program Files itself made sandboxed React, Nuxt and other Node services fail with "Access is denied".
  Pythonator now says so before the start, and **System check** offers **Let sandboxes use it…**, one
  administrator prompt that lets sandboxes read and run (never change) that folder.
- Project builder 1.1: Discord, Telegram and WhatsApp bots in Python or Node.js (discord.py, discord.js,
  python-telegram-bot, grammY, and a signature-checked webhook for Meta's official WhatsApp Cloud API), an Express
  API and a Python worker, next to the web frameworks. A bot's token goes in its Environment table. The panel has
  **↻ Refresh**, lists the projects it made and can delete one: Pythonator asks you first, in its own window, and
  only deletes a folder it made itself.
- Django projects use 5.2 LTS (5.2.19 or newer), Flask 3.1.3 or newer and FastAPI 0.162 or newer.
- The CPU and RAM meter shows Pythonator's own share (this window, the background service, plugins and every
  service) as the brighter part of each bar, with the figures in its tooltip.
- The Plugins window keeps every plugin's page in its width: long permission texts and setting names wrap.
- Docker Compose on Windows: the compose plugin of Docker Desktop is found again (its folder was missing from the
  environment of Pythonator's compose check), and an old or missing Compose is named as such.

## 0.0.11

- **Notify**, a new official plugin, replaces Health checks and Webhooks (your settings move over by themselves):
  one timeline for all services, or the ones you pick, of crashes, restarts given up, blocked starts, failed
  installs, failing health checks, recoveries and anomalies (memory that keeps growing, CPU stuck high, a crash loop,
  bursts of errors, a server's port gone). Alerts show in the window, as a tray pop-up while it is hidden, and on
  Discord, Slack, Telegram, Teams, ntfy or any URL. A service can be muted for a while.
- **Project builder**, a new official plugin: a React, Vue, Nuxt, FastAPI, Flask or Django project in one click,
  made by each framework's own tool (`npm create vite`, `create-vue`, `create-nuxt`, pip and `django-admin`) in the
  new service's own sandbox, and added as a service on a free port for this computer only. Plugins can ask for this
  with the new `create_services` permission (plugin API 4).
- **Docker Compose projects**: a folder with a `compose.yaml` runs as one service in Container mode. Start runs
  `compose up` with every container's output in the console, Stop runs `compose stop`, and **Compose ▾** builds,
  pulls, lists and takes it down. Before each start and build the file is checked as the engine reads it, and
  privileged containers, host namespaces, devices, mounts outside the folder, the engine's socket and ports open to
  every network are refused with the reason.
- Plugin panels are docks: tabs side by side, or **⧉** for a real window of their own (any screen, minimise and
  maximise). **Plugins ▾** shows or hides each one, gathers them back as tabs, and **Lock panel layout** keeps
  them in place. The layout is remembered.
- The AI assistant (1.2) shows its whole answer, however long, formatted as Markdown (headings, lists, tables,
  code), instead of cutting it with "the rest is cut in the panel". A plugin message may now be up to 1 MB.
- Smoother window: panel text no longer re-wraps on every pixel of a resize, plugin tables are updated in place
  instead of rebuilt and measured, and the console doesn't redraw while you drag or resize the window.
- Editor: saving no longer adds blank lines to Windows (CRLF) files, pasted text takes the file's own line endings,
  and Undo right after opening no longer empties the file. A file that isn't UTF-8 opens read-only, with a note,
  instead of being changed on save.
- Service window: the Environment and Folders tables have **＋ Add** and **− Delete** buttons (and the Delete key),
  and **Import from env file** copies its names into the table.
- **System check** is redone: what the computer is (Windows edition and build, macOS or Linux distribution,
  processor, cores, memory, disk), what it is doing now, which runtimes and container engines are installed with
  their versions, and which sandbox protections are enforced, in cards. **Copy report** leaves out your computer
  and user names.
- Windows: a plugin no longer shows an "Unhandled exception in script" window (BrokenPipeError) when Pythonator
  closes or restarts it; it just ends.

## 0.0.10

- The editor's find bar is redone: icon buttons with tooltips, a match counter ("3 of 18"), every match highlighted,
  match case, whole word and regex toggles, previous and next, and a replace row with Replace and Replace all
  (one undo step). Ctrl+F finds, Ctrl+H replaces, Enter and Shift+Enter step, F3 too, Esc closes.
- AI assistant 1.1: a real chat window. Your questions and its answers are bubbles in their own scrolling view that
  follows the answer as it streams (scroll up and it stays put), code shows as code, errors are short. Quick
  questions are one menu under the input.
- AI assistant: a **Model** list per AI service, fetched from the service, kept for a day and refreshed with
  **↻ Refresh** (which also tests the connection). A misspelt model name is pointed out instead of failing with a 404,
  and Gemini's errors read plainly.
- Plugins: panels can show a chat (`ui.chat`, `ui.message`; plugin API 3).

## 0.0.9

- Windows: uninstalling ends every Pythonator process still running from the install folder (a window, or a
  plugin or sandbox launcher left behind), so Pythonator.exe can always be deleted. The installer test now lists
  what was still running and shows the uninstall log when something is left.

## 0.0.8

- Windows: Full access Python services no longer exit at once with code 106. The venv's small `python.exe`
  launcher is skipped: Pythonator starts the venv's real Python, which finds the venv itself. A crash with one of
  that launcher's codes now says what it means.
- The console's input has a **stdin / shell** switch (Ctrl+\`). Shell runs a command (`pip list`, `npm ls`, `dir`)
  in the service's folder, in its own sandbox or container, with its environment first on PATH, while the service
  keeps running.
- Custom start commands run as typed, without a shell, and take their program from the project's `.venv` (and
  `node_modules/.bin`) before PATH, as in an activated environment. Python, Node and custom services get that
  PATH too, and Pythonator's own venv no longer leaks into a service that runs on the computer's Python.
- Selecting a service shows its log already at the bottom, instead of scrolling through it.
- Plugins read what Pythonator sends them as UTF-8 on every Windows code page. This fixes the AI assistant's
  "surrogates not allowed" error.
- Health checks ask the port a server really listens on, when it isn't the one in its settings.
- Guard names its whole-computer rule clearly instead of showing "computer overload".

## 0.0.7

- Windows: starting, stopping or restarting a service no longer turns the mouse pointer into the busy one. Every
  process Pythonator starts (services, setup steps, the polite stop, git, plugins, the sandbox launcher) now tells
  Windows not to show "app starting" feedback.

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
