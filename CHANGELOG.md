# Changelog

All notable changes to Pythonator are listed here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Dates are UTC. Plugin and plugin API versions are given where they
changed.

## [0.0.16] - 2026-11-06

Bundled plugins: Project builder 1.4.0, Notify 1.1.1, AI assistant 1.3.1.

### Security

- Git pull: a checkout whose `.git/config` holds settings outside an allow-list (`core.sshCommand`, filter
  drivers, `credential.helper`, `include.path` and others) is refused, naming each setting. A sandboxed service
  could write these to run a program outside the sandbox on the next pull. A sandboxed service must be stopped
  before a manual pull. The empty hooks folder git runs with moved from `/tmp/pythonator-no-hooks` into the
  private data folder.
- The Python version check before a start reads `pyvenv.cfg` instead of running the project's `.venv` Python
  with `--version` outside the sandbox.
- Sandbox read grants outside the project come only from a real Python installation. `home` in `pyvenv.cfg`, or
  a link in `.venv`, could grant read access to any folder.
- Env files: read only when the file resolves inside the project folder (a link to `~/.aws/credentials` loaded
  the keys). An env file can no longer set `PATH`, `HOME`, `LD_*`, `DYLD_*`, `DOCKER_*`, `BUILDKIT_*`,
  `CONTAINER_*`, `CONTAINERS_*`, `PODMAN_*`, `XDG_*`, `GCONV_PATH`, `LOCPATH`, `PYTHONHOME` or `PYTHONPATH`; these
  reached the sandbox launcher, bubblewrap and the Docker CLI, which run outside the sandbox. Variables set on the
  service itself are unchanged.
- Inherited environment variables named like secrets (`GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `*_API_KEY`) are
  no longer passed to sandboxed services or plugins. Console masking is rebuilt at each start from the variables
  actually passed, including a changed `.env`.
- Sandbox **More folders**: a folder that contains a folder of keys (for example `~/.config`, which holds `gh` and
  `gcloud`) is refused.
- Linux: Landlock-only sandboxes read only the needed device files (no cameras, microphones or `/dev/pts`);
  `/dev/pts` is available only inside bubblewrap. With bubblewrap, the network on and Landlock below ABI 6 (Linux
  before 6.12), seccomp blocks new Unix sockets, which reached abstract sockets such as X11's.
- macOS: the Seatbelt profile no longer allows `/dev/ttys*`.
- Compose: named volumes with `driver_opts` binding a host folder outside the project, and networks named or
  driven by `host`, are refused.
- Container mode refuses a project path that contains a comma or a quote, which could add a second mount source.
- Plugins: `read_files` and `propose_changes` refuse any path through a symbolic link (a link to `id_rsa` or
  `.git` was followed). An edit that touches a hidden value gets the same error as text that is missing or appears
  twice, so a value can't be guessed one character at a time. Proposals are refused when they add CI workflows
  (`.github/`, `.gitlab-ci`), npm install scripts, renamed imports (`from os import system as s`), `getattr` with a
  built-up name, or `__builtins__`.
- Plugins: removing a plugin, or installing different code under the same id, clears its grants and live mode.
  Plugin files that change on disk after the trust check are treated as new code: off, in test mode, with no
  grants.
- Plugins: limits by size as well as count (2 MB/s), 50 stderr lines/s, 2,000 characters per log line, and one
  `hello` per process (a second one re-read the keychain).
- Plugin, program and git text is shown as plain text in labels, message boxes and tooltips. Qt rich text in a
  proposal path (`<img src='/dev/zero'>`) hung the window.
- Windows: a process that could not be added to its job object is never resumed.
- The background service's token file is written only after it listens.

### Added

- Single instance per data folder (Windows, macOS, Linux): starting Pythonator again brings the open window to the
  front; a start at sign-in exits quietly. Only one background service runs.
- Linux: the sandbox launcher is a child subreaper, and Stop also ends processes that left the service's process
  group (`setsid`, double fork).
- Project builder 1.4.0: setting **Refresh looks for leftover project folders** (off by default) lists unused
  folders in the projects folder; **Clean up N leftover folders…** deletes the ticked ones after a confirmation.
  SDK calls `leftover_projects` and `delete_leftovers`.
- Right-click **Remove** on a Project builder project offers **Delete project and folder** or **Remove service,
  keep folder**, as the builder's Delete does. One question covers several selected services.
- Editor: saving over a file changed on disk since it was opened asks first.

### Changed

- **F5** only starts a service; Ctrl+R restarts.
- Plugin settings: labels wrap to any number of lines; yes/no settings are a check box with the label beside it.
- AI assistant 1.3.1: edit blocks are parsed as models write them (missing `>>>>>>> END`, `>>>>>>> REPLACE`,
  blocks inside code fences, `<<<<<<< SEARCH` under the file name, CRLF). Each change is tried against the file
  before it is offered, with whitespace and indentation differences matched; a change that would fail is not
  offered and the chat names the failing edit. One **Review** button per file, from the latest answer.
- Messages to plugins are written by one thread per plugin with a 2,000-message queue; a plugin that falls behind
  is stopped and turned off.
- Typed input to a service is written by its own thread; more than 256 unread lines are refused.
- Container services are prepared and started off the background service's main thread.
- Stats: one process-table pass and one connections call per round for all services (17.7 ms per service with 600
  processes before). `running.json` is written only when the set of running services changes.
- Console search marks matches on screen only, after typing pauses, and counts the rest (353 ms per keystroke on a
  long log before). A hidden console polls 4 times a second instead of 60.
- Project builder 1.4.0: takes service state from events instead of reloading all templates and projects on every
  start and stop; template checks run off the main loop with one lookup per tool.
- Opening a file reads only its first 8 KB to detect the encoding.

### Fixed

- A plugin that stopped reading its messages froze the background service.
- Typing into a service that doesn't read its input blocked the background service (from 131 KB on Linux, about
  4 KB on Windows).
- A slow Docker or Podman (Docker Desktop still starting) stalled all services and the window for up to about 90 s.
- Stop or Remove during pull-on-start still launched the service; a double Start created two runners.
- Closing a modified tab with **Save** when the save failed closed the tab and lost the edits.
- A new background service exited while the old one was shutting down, leaving the window on "connecting".
- Two windows or two background services could run at once, so every question was asked twice and every service
  ran twice.
- Removing a service a second time (a second click or window, while it stops or after it is gone) force-killed it
  or showed "No such service".
- Windows: Project builder Refresh could offer a leftover folder again while it was being deleted.
- AI assistant: a missing `>>>>>>> END` wrote the next edit block's markers into the file. Pythonator itself now
  refuses any change that adds an edit marker or a hidden-value placeholder.
- Notify 1.1.1: a `Retry-After` header with a date stopped alert delivery; HTTPS webhooks could redirect to HTTP.
- Windows: replacing the private Node.js copy no longer removes one that a running service still uses.
- White ANSI text was invisible on the light theme.
- Header icons are redrawn after a display scale change.
- A plugin name longer than 64 characters is truncated in API errors.

### Removed

- Unused code: `git.version()` and `MIN_VERSION`, `Supervisor._pids`, `launcher.windows_profile_sid` and
  `windows_acl_sids`, `FileTree.status`, `MainWindow.header`, `ansi.strip`, `PluginHost._write_raw`,
  `NODE_OPTIONS_PRELOAD`.

## [0.0.15] - 2026-11-06

Bundled plugins: Project builder 1.3.0.

### Added

- Project builder 1.3.0: 38 templates and a **Runtime** filter. New: React + TypeScript, Svelte, Solid, Preact and
  Vanilla JS on Vite; Next.js, Angular and Astro; Fastify, Hono and Koa; Litestar, Streamlit and Gradio; Go
  (net/http, Gin), Rust (Axum), Bun (Hono, Elysia), Deno, Java, PHP and Ruby (Sinatra); a Node.js worker.
- Runtime version checks before a start, for every runtime, from the template's requirements and the project's
  `package.json` engines, `requires-python`, `go.mod`, `rust-version` and composer's `php`. The message names the
  required and installed versions.
- A start notes a listed port already in use (and by which program) or, on Windows, reserved for Hyper-V, WSL or
  Docker. A service not listening on its port after 30 s gets a note with what its processes are doing.
- Windows: the start says when a sandboxed server is not yet reachable from this PC, and **Open** offers **Make
  reachable** first.
- CI starts a Vite app in the Windows sandbox on every push, makes it reachable and loads its page.

### Changed

- The plugin dock is laid out once resizing or moving stops (97 ms to 5 ms per drag step with a long chat); plugin
  updates are drawn at most every 60 ms, and a hidden tab's only when shown (91 ms to 13 ms per streamed AI piece).
- The console shows the command that actually runs, including the private Node.js copy in the Windows sandbox.
- Nuxt runs with `--no-fork`, Astro's dev server stays in the foreground, and Astro's starter is generated locally
  instead of downloaded from GitHub.

### Fixed

- Windows sandbox: Node.js hung at 100% of a core when a program started a child process (Vite, Next.js, Angular,
  npm install scripts). The sandbox's private Node.js copy renames libuv's pipe prefix `\\?\pipe\uv\` to
  `\\?\pipe\LOCAL\`, as libuv 1.53 does.
- Windows sandbox: Vite stopped at *EPERM: operation not permitted, realpath*; the Node.js preload answers with the
  existing path when Windows refuses `realpath`.
- Linux sandbox: services with listed ports may also bind a system-chosen port (port 0), which Next.js Turbopack and
  Nuxt workers need.
- Rust installed with rustup works in the sandbox (toolchains folder readable, `RUSTUP_HOME` set).
- Nuxt failed with Node.js 22.13: Nuxt 4 needs 22.22.3 or newer, now reported before the start.

## [0.0.14] - 2026-11-06

Bundled plugins: Project builder 1.2.1.

### Changed

- Revoking a sandbox's folder permissions after Remove or Delete runs in the background and skips a folder about to
  be deleted (minutes over a large `node_modules` on Windows before).
- The first sandboxed Node.js start copies Node.js in the background, with a console note.
- Project builder 1.2.1: one **Create** button; an empty name uses the suggested one.

### Fixed

- Windows sandbox: npm and Node.js failed with *EPERM: operation not permitted, lstat 'C:\'* (create-vite,
  create-vue and other Node templates). Node.js starts with `--preserve-symlinks --preserve-symlinks-main` and a
  preload that answers for folders above the sandbox's own.
- Windows: deleting a project now deletes its folder: open files are closed and the folder unwatched first, paths
  over 260 characters are handled, and files briefly held by a virus scanner are retried.
- Windows CI tests the "Let sandboxes use it" check as it works since 0.0.13.

## [0.0.13] - 2026-11-06

Bundled plugins: Notify 1.1.0, Project builder 1.2.0, AI assistant 1.3.0. Plugin API 5.

### Added

- Plugin API 5: views (`ui.tabs`) and icon buttons (`ui.button(..., icon="refresh")`).
- Notify 1.1.0: **Services** and **Events** views. Project builder 1.2.0: **New project** and **Projects** views.
- Header **Details (i)**: process, CPU, memory, ports, isolation, restart policy and folder; selecting a line copies
  it. Also in the service list's context menu.

### Changed

- Plugin panels can't exceed the dock: text and paths wrap, buttons elide with "…", drop-downs fill the row, and the
  dock is at least 240 px wide.
- The header is one line with icon buttons (Start, Stop, Restart, Packages, Configure, Details, More).
- The editor has no toolbar of its own (New, Open and Save sit beside its tabs), lower tabs, and icon buttons in the
  console.

### Fixed

- Windows sandbox: Node.js projects failed with "Access is denied" when Node.js was installed for all users in
  `C:\Program Files\nodejs` (0.0.12's check never fired). Sandboxes run a private Node.js copy, made once per
  version in the data folder, and npm runs without a batch file.
- AI assistant: edits for the prompt's example path (`path/to/file.py`) or the wrong folder go to the one file that
  holds the replaced text; trailing spaces are ignored when matching; an edit that matches no file is dropped with a
  note instead of creating a new file.

## [0.0.12] - 2026-11-06

### Added

- Project builder: Discord, Telegram and WhatsApp bots in Python or Node.js (discord.py, discord.js,
  python-telegram-bot, grammY, a signature-checked WhatsApp Cloud API webhook), an Express API and a Python worker;
  **↻ Refresh**, a project list, and Delete (only for folders the builder made, after a confirmation).
- Windows: **System check** offers **Let sandboxes use it…** for a runtime folder sandboxes can't read: one
  administrator prompt grants read and execute.
- The CPU and RAM meter shows Pythonator's own share (window, background service, plugins and services).

### Changed

- Plugin panels are tabs in one Plugins dock again; no separate windows or layout lock.
- Panel rows of cards, lists or buttons wrap; wide tables scroll horizontally.
- Templates: Django 5.2 LTS (5.2.19 or newer), Flask 3.1.3 or newer, FastAPI 0.162 or newer.

### Fixed

- The console stuttered while the window is resized or moved (since 0.0.11).
- AI answers cut off at the bottom of their bubble; the note under a question overlapped it.
- Windows: Node.js in `C:\Program Files\nodejs` without the sandbox read entry failed with "Access is denied"; the
  start now reports it.
- The Plugins window kept wide permission texts and setting names from wrapping.
- Docker Compose on Windows: Docker Desktop's compose plugin was not found; an old or missing Compose is reported.

## [0.0.11] - 2026-11-06

Bundled plugins: Notify 1.0.0, Project builder 1.0.0, AI assistant 1.2.0. Plugin API 4.

### Added

- Notify plugin, replacing Health checks and Webhooks (settings migrate automatically): one timeline of crashes,
  given-up restarts, blocked starts, failed installs, failing health checks, recoveries and anomalies (growing
  memory, CPU stuck high, crash loops, error bursts, a lost port). Alerts in the window, as tray notifications, and
  on Discord, Slack, Telegram, Teams, ntfy or any URL. Services can be muted for a time.
- Project builder plugin: React, Vue, Nuxt, FastAPI, Flask or Django projects made by each framework's own tool in
  the new service's sandbox, added on a free local port. Plugin permission `create_services` (plugin API 4).
- Docker Compose projects as one service in Container mode: `compose up`, `compose stop`, and **Compose ▾** for
  build, pull, ps and down. The resolved file is checked before each start and build; privileged containers, host
  namespaces, devices, mounts outside the folder, the engine socket and ports open to all networks are refused.
- Plugin panels as docks, tabbed or floating, with a remembered layout.
- Service window: **＋ Add** and **− Delete** in the Environment and Folders tables, and **Import from env file**.
- **System check** redesigned: system, current load, installed runtimes and engines with versions, and enforced
  sandbox protections. **Copy report** omits computer and account names.

### Changed

- AI assistant 1.2.0: full answers rendered as Markdown (plugin API 4). Plugin messages may be up to 1 MB.
- Panel text no longer re-wraps on every pixel of a resize; plugin tables update in place; the console doesn't
  redraw while the window is dragged or resized.

### Fixed

- Editor: saving added blank lines to CRLF files; pasted text keeps the file's line endings; Undo right after
  opening emptied the file. Non-UTF-8 files open read-only with a note.
- Windows: a plugin showed "Unhandled exception in script" (`BrokenPipeError`) when closed or restarted.

## [0.0.10] - 2026-11-05

Bundled plugins: AI assistant 1.1.0. Plugin API 3.

### Added

- Editor find bar: match counter, highlight of all matches, match case, whole word and regex, and a replace row
  (Replace all is one undo step). Ctrl+F, Ctrl+H, Enter/Shift+Enter, F3, Esc.
- AI assistant 1.1.0: chat view with streamed answers, code blocks and short errors; a **Model** list per service,
  fetched, cached for a day and refreshed with **↻ Refresh**, which also tests the connection.
- Plugin API 3: chat panels (`ui.chat`, `ui.message`).

### Fixed

- AI assistant: a misspelt model name is reported instead of failing with a 404; Gemini errors are readable.

## [0.0.9] - 2026-11-05

### Fixed

- Windows: uninstalling ends every Pythonator process still running from the install folder, so `Pythonator.exe`
  can always be deleted. The installer test lists leftover processes and shows the uninstall log.

## [0.0.8] - 2026-11-05

### Added

- Console input switch **stdin / shell** (Ctrl+\`): shell runs a command in the service's folder, sandbox or
  container, with its environment first on `PATH`, while the service keeps running.

### Changed

- Custom start commands run without a shell and resolve their program from `.venv` and `node_modules/.bin` before
  `PATH`. Python, Node.js and custom services get the same `PATH`; Pythonator's own venv no longer leaks into them.
- Selecting a service shows its log scrolled to the bottom.

### Fixed

- Windows: Full access Python services exited at once with code 106. The venv's `python.exe` launcher is skipped
  and its exit codes are explained.
- Plugins read Pythonator's messages as UTF-8 on every Windows code page ("surrogates not allowed" in the AI
  assistant).
- Health checks use the port a server actually listens on.
- Guard names its whole-computer rule correctly.

## [0.0.7] - 2026-11-05

### Fixed

- Windows: starting, stopping or restarting a service showed the busy mouse pointer. Every process Pythonator
  starts disables "app starting" feedback.

## [0.0.6] - 2026-11-05

### Added

- **Settings**: start services one at a time (default), a few at a time, or all at once; log rotation size and
  count; keep running in the tray when the window closes; start at sign-in, in the tray.
- Service list: multi-select with Ctrl or Shift, reorder with ↑ ↓ or Alt+↑ / Alt+↓, and start, stop or remove
  several from the context menu.

### Removed

- The separate start-order window.

### Fixed

- Two services in one folder created its `.venv` at the same time.

## [0.0.5] - 2026-11-05

### Added

- Start queue for **Start all** and sign-in: list order, 4 at a time by default (**Settings → Start at once**).
- **Settings**: start order and removing several services.

### Changed

- A first start skips setting up pip when the project has nothing to install.

## [0.0.4] - 2026-11-05

### Changed

- The console spreads output over every frame (60 per second, 30 under a flood); the "skipped" note appears at
  most once a second.
- Output is read in chunks instead of line by line (84% to 7% of a core in a flood test), and the console caps what
  one frame draws.

## [0.0.3] - 2026-11-05

### Fixed

- Sandbox mode: setup steps couldn't write the project's `.venv`, which was also bound read-only; own environments
  are now made and filled on every system. On Windows, pip is set up as a separate step.

## [0.0.2] - 2026-11-05

### Added

- **Own environment** per service, on by default: Start creates `.venv`, `node_modules` or `vendor` when missing;
  Deno, Go, .NET and Rust get a private package cache. **Packages ▾ → Recreate environment** starts it fresh.
- **Start all** and sign-in start services one after another.

### Fixed

- The editor stalled while a service flooded its console. The console draws a few milliseconds per frame, pauses
  during typing and scrolling, thins floods (the log keeps everything) and handles carriage returns.
- Plugin panel redraws took keyboard focus from the editor.

## [0.0.1] - 2026-11-05

First release from the relaunched repository. Services and settings from preview builds carry over. Plugin API 2.

### Added

- Services with restarts and back-off, schedules (intervals or cron), memory and CPU limits for the whole process
  tree, live CPU, memory and ports, and rotating logs with secrets masked. Services keep running when the window
  closes.
- Sandbox mode by default (Landlock, seccomp and bubblewrap on Linux; Seatbelt on macOS; AppContainer and a job
  object on Windows), Container mode with hardened Docker or Podman, and Full access after confirmation.
- Per-service ports, local only or network-wide, and **Make reachable** on Windows.
- Python, Node.js, Bun, Deno, Go, Rust, Java, .NET, Ruby, PHP, PowerShell, shell scripts or any command.
- Sandboxed plugins: Webhooks, Guard, Health checks and the AI assistant.
- Built-in editor, Git clone and pull, and installers for Windows, macOS and Linux (x64 and arm64).

## Preview builds

Released before the relaunch as 0.0.1 to 0.0.11. Downloads are retired; the notes are kept for reference.

### Preview 0.0.11

#### Added

- Per-service **CPU limit** in percent of one core for the whole process tree. Windows: job object hard cap.
  Containers: `--cpus`. Linux and macOS: the tree is paused in short slices when over the limit, re-measured twice
  a second.
- The console lists a service's limits and how they are enforced at each start; the header shows CPU use against
  the limit.
- Guard: when the computer stays over 95% CPU or 92% memory for 30 s, the service using the most gets Guard's
  action, if it uses at least a quarter of all cores or a tenth of memory. Both thresholds are configurable.
- AI assistant, rebuilt as a sandboxed official plugin (off by default): Claude, OpenAI, Gemini, DeepSeek, Ollama or
  any OpenAI-compatible server. Suggested changes open a review window; nothing is written without Apply.
- Plugin API 2: text box, drop-down and code block panels; `read_files` (read-only, secrets hidden) and
  `propose_changes` permissions. Changes to scripts, emptying files, and code that runs shell commands, deletes files
  or evaluates strings are refused.
- `inspect` plugins get the computer's CPU and memory (`self.system`) and each service's `cpu_limit`.
- **Help ▾ → About Pythonator** with credits, support link and versions; Help ▾ opens the documentation.

#### Changed

- Plugin `secret` settings are stored in the system keychain (Windows Credential Manager, macOS Keychain, Secret
  Service), not `settings.json`; existing secrets move on their next save.

### Preview 0.0.10

A rewrite of the engine under 0.0.9's window: about 10,000 lines instead of 24,000, typed and checked with
`mypy --strict`.

#### Added

- A background service runs services independently of the window; crashes of either leave services running and
  leftovers are cleaned up.
- Restart back-off from 1 s to 60 s with a limit per 10 minutes; schedules with intervals, `@daily` or cron; a
  memory limit for the whole process tree.
- Package installs inside the service's isolation (`.venv`, `node_modules`, `vendor`). Rust and PowerShell
  runtimes.
- Environment check before each start; **System check** for the sandbox protections enforced.
- Isolation modes **Sandbox** (default), **Container** and **Full access**; a service doesn't start if a protection
  can't be applied. Linux: Landlock (files, TCP ports, scopes), seccomp and bubblewrap. macOS: Seatbelt. Windows:
  AppContainer with recorded, reversible folder grants, and a job object.
- Ports per service, local only or other computers too. Windows **Make reachable**: loopback exemption, loopback
  permit and firewall rules for that service only; **Undo** removes them.
- Containers with all capabilities dropped, `no-new-privileges`, a read-only image, a process limit, and optional
  container networks.
- Plugins in sandboxed processes with a checked protocol; official plugins verified by SHA-256 from a reviewed
  catalog; third-party plugins start in test mode with no permissions and are disabled on floods, invalid messages
  or repeated crashes. Permissions `network`, `read_logs`, `inspect`, `control`, `policy`, `ui`. Official plugins
  Webhooks, Guard and Health checks.
- QScintilla editor; hardened Git clone and fast-forward-only pull; documented API with `docs/examples/pyctl.py`.
- Installers: Windows `setup.exe` and `.zip`, macOS `.dmg` (Apple silicon and Intel), Linux `.deb` (x64, arm64)
  and `.tar.gz`. Updates stop the background service first; Windows uninstall removes sandbox folder permissions.
- CI on Windows, macOS and Linux with installer tests; `pip-audit` on hash-pinned lock files; attested releases.

#### Changed

- The console draws at most 10 ms per frame and skips what doesn't fit (noted; the log keeps everything); only the
  shown service's output reaches the window; fast writers block on their pipe instead of building a backlog.
- One editor zoom for all files (Ctrl+, Ctrl-, Ctrl+=, Ctrl+wheel, Ctrl+0 resets), remembered.
- Start all and Stop all return at once; a second Stop kills what is still stopping.

#### Removed

- The AI assistant plugin, plugin `pyproject.toml` packaging, `bot.json` and other old-format migrations, the
  in-app API reference generator, and the code signing scripts. 0.0.9's `pythonator.json` is still imported once.

### Preview 0.0.9

#### Added

- Windows sandbox: **Make reachable…** adds a firewall rule admitting this PC only, so a sandboxed server answers
  on `http://127.0.0.1:<port>/`. Services set up with 0.0.8 or earlier need it once.
- Dependencies menu for Python: **Create venv**, **Recreate venv…** and a **Use .venv** switch; other runtimes
  show where their packages go.

#### Changed

- A created venv is used from then on, even if the service was set to another Python.

### Preview 0.0.8

#### Added

- Python services ask before running outside the project's venv (none, unused or broken), with a per-service
  remembered choice.
- `$` commands that install account-wide (`npm -g`, `yarn global`, `gem install`, `composer global`,
  `pip --user`) ask first and offer the project-local command.

#### Changed

- After creating or recreating the venv, the requested start, install or command runs in it.

### Preview 0.0.7

#### Fixed

- Windows sandbox: "working folder read only" was not enforced for services previously set to "read & write";
  writes allowed by another folder's permissions are blocked too.

### Preview 0.0.6

#### Removed

- The command line added in 0.0.5.

### Preview 0.0.5

#### Added

- Dependency vulnerability checks on every change and weekly; Dependabot for the app's and official plugins'
  requirements.

#### Fixed

- Windows sandbox: temporary files go to the sandbox's own temp folder instead of the service's folder.

### Preview 0.0.4

#### Fixed

- Windows sandbox: first attempt at keeping temporary files out of the service's folder (completed in 0.0.5).

### Preview 0.0.3

#### Added

- Guard plugin rewritten as a security policy: 19 rules for isolation, exposed ports, outgoing connections,
  container images and a resource watchdog; each can warn, stop or restart, with per-service exceptions.
- Webhooks plugin: Discord, Telegram, WhatsApp, Slack, Teams, ntfy, Pushover, Gotify or a custom endpoint, batched
  per service and rate-limited.
- Plugin SDK 2: service setup and resource use; multi-line and tabbed settings.
- Outgoing connections are recorded per service with its listening ports.

#### Fixed

- Windows sandbox: pip installs and Python's `tempfile` work.

### Preview 0.0.2

#### Fixed

- Windows sandbox: **Create venv** works and the venv always has pip; **Install dependencies** adds pip to a venv
  without it; `$` commands run in cmd (PowerShell can't run in the sandbox); services no longer inherit
  Pythonator's DLL search path or internal variables.

### Preview 0.0.1

First public release.

#### Added

- Services in Python, Node.js, Bun, Deno, Go, Java, .NET, Ruby, PHP, shell, PowerShell or any program.
- Restarts with back-off and a crash-loop limit, schedules, memory and CPU limits, autostart and start at sign-in.
- A background service keeps services running while the window is closed.
- A sandbox per service on Linux, macOS and Windows, and Docker / Podman containers. See
  [Security](docs/security.md).
- An editor with highlighting and completion, a console with colours, rotating logs and history.
- Git clone and pull before each start.
- Plugins: official ones verified against a reviewed catalog; third-party ones always sandboxed. Official plugins:
  Webhooks, Guard and the AI assistant (Claude, OpenAI, Gemini, DeepSeek, Ollama).
- A website with guides, a glossary, a dependency list and an API reference.

[0.0.16]: https://github.com/cfunkz/Pythonator/compare/v0.0.15...v0.0.16
[0.0.15]: https://github.com/cfunkz/Pythonator/compare/v0.0.14...v0.0.15
[0.0.14]: https://github.com/cfunkz/Pythonator/compare/v0.0.13...v0.0.14
[0.0.13]: https://github.com/cfunkz/Pythonator/compare/v0.0.12...v0.0.13
[0.0.12]: https://github.com/cfunkz/Pythonator/compare/v0.0.11...v0.0.12
[0.0.11]: https://github.com/cfunkz/Pythonator/compare/v0.0.10...v0.0.11
[0.0.10]: https://github.com/cfunkz/Pythonator/compare/v0.0.9...v0.0.10
[0.0.9]: https://github.com/cfunkz/Pythonator/compare/v0.0.8...v0.0.9
[0.0.8]: https://github.com/cfunkz/Pythonator/compare/v0.0.7...v0.0.8
[0.0.7]: https://github.com/cfunkz/Pythonator/compare/v0.0.6...v0.0.7
[0.0.6]: https://github.com/cfunkz/Pythonator/compare/v0.0.5...v0.0.6
[0.0.5]: https://github.com/cfunkz/Pythonator/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/cfunkz/Pythonator/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/cfunkz/Pythonator/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/cfunkz/Pythonator/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/cfunkz/Pythonator/releases/tag/v0.0.1
