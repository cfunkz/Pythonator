# Changelog

All notable changes to Pythonator are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dates are UTC. Bundled plugin and
[plugin API](docs/plugin-api.md) versions are given where they changed.

## [Unreleased]

## [0.0.19] - 2026-11-07

### Added

- A start refused because a port is in use or reserved by Windows (Hyper-V, WSL, Docker) names a free port near it.
  Make reachable refuses a reserved port.
- A running server gets a note when it listens on IPv6 only (`[::1]`, what `localhost` can mean), since the Open
  button, Make reachable and `HOST` use `127.0.0.1`, and when another program listens on the same port and may take
  its connections.

### Changed

- Container mode's default images: Deno `denoland/deno:debian` (the `2` tag never existed, so Deno couldn't start),
  Go `golang:1.27` (1.24 has ended), .NET `sdk:10.0` (9.0 ends on 2026-11-10), Node.js `node:24-slim`.
- Container mode keeps Go, Rust, Ruby, Deno and NuGet packages in the project's `.container-cache` folder, so
  installs work with the read-only image. PHP installs run in the `composer:2` image (`php:*-cli` has no Composer).
- Project templates ask for supported runtimes: Node.js 22, Python 3.11, Go 1.26, Ruby 3.3, PHP 8.3, Java 21. The
  discord.py template requires aiohttp 3.14.5 or later.
- The discord.py, discord.js and grammY bot templates log out cleanly when stopped on Linux and macOS (SIGTERM).
- The file tree shows `node_modules`, `.venv`, `.git`, `target` and similar folders without listing them: a big
  `node_modules` held the window for over a second each time it was opened.

### Fixed

- A crashed service whose child still held its output (a worker pool, an ffmpeg) showed as running and was never
  restarted. The exit is now seen when the program itself ends, and what it left behind is ended 2 s later.
- An error that nothing caught, anywhere in the window or the background service, ended the whole program (PyQt's
  default). It is now written to `window.log` or `daemon.log`, and the window says so in its status bar.
- System check: long paths wrap instead of pushing the dialog past its window, the note at the bottom wraps, and
  paths in your home folder show as `~` (they showed your user name).
- Ending a plugin ends every process it started: its process group, and on Windows its own job object (the
  sandboxed worker outlived the launcher before).
- Linux: in Docker or LXC, where bubblewrap can make a user namespace but not mount `/proc`, every sandboxed start
  and plugin failed. The bubblewrap check now tries what a start does, so Sandbox mode falls back to Landlock and
  seccomp there.
- A server with forked workers (gunicorn, uvicorn `--workers`, PHP-FPM, Node.js cluster) was stopped far below its
  memory limit: the memory the workers share with their parent was counted once per worker (300 MB with four
  workers counted as 1.5 GB). Before stopping a service, Linux now counts shared memory once (PSS) and Windows counts
  private memory, the same as its job object.
- The file tree's icon provider could be freed before the tree's model, which still pointed at it; the next event
  crashed (macOS CI). It now lives as long as the program.
- A memory or CPU limit typed with hundreds of digits raised an error instead of saying what to write.
- A plugin disabled for misbehaving: the messages it had already sent are dropped. They were still acted on (one
  could stop a service), and each disabled it again, with a settings save and a broadcast every time.

### Security

- Linux: nothing a sandboxed service starts can leave its process group. seccomp makes `setsid` and `setpgid` do
  nothing, so Stop reaches a child that started a new session and kept changing its process ID. Without bubblewrap
  such a child outlived Stop and could rewrite `.git/config` while git pulled the folder. macOS finds such children
  by a marker in their environment.
- git: `GIT_ALLOW_PROTOCOL=https:ssh`. A checkout's own `protocol.ext.allow=always` beat the command-line pin.
- A pull also waits for, or refuses while, a sandboxed service that may write the checkout through *More folders*.
- Official plugins no longer run without a sandbox where it can't be set up (0.0.18 ran them with your full rights
  on Linux with Landlock but neither libseccomp nor bubblewrap). They show why they didn't start.
- Windows: WSL's `bash.exe` and `wsl.exe` in System32 are refused in Sandbox mode. They would run the script inside
  WSL, outside the AppContainer.
- Files a sandboxed service can write in its folder and Pythonator reads itself (`package.json`, `pyproject.toml`,
  `go.mod`, `Cargo.toml`, `composer.json`, `pyvenv.cfg`, the env file, and files plugins read): only regular files
  of at most 1 MB are read, without waiting. `mkfifo package.json` made the background service wait forever at the
  next start, which froze every service, and a file of gigabytes or a link to `/dev/zero` could fill its memory.
- Release builds install Pythonator with `--no-build-isolation`, so setuptools comes from the hash-locked build lock
  instead of an unchecked download.

## [0.0.18] - 2026-11-07

Bundled plugins: AI assistant 1.4.0.

### Added

- AI assistant: an answer that lays out a project (a changed `main.py`, a new `static/index.html`, a new
  `static/js/game.js`) is one **Review changes to 3 files** button and one review window that lists every file
  with its diff. **Apply all** writes them all, making new folders, or none. Each file follows the usual rules: no
  `.env` or other secret files, no scripts, nothing outside the service's folder or through a link. A new file
  named like `script.js` is no longer taken for the prompt's example path and dropped, and the prompt asks for one
  edit block per file instead of a folder tree. Plugins get `propose_changes` for this (see the plugin API).

### Changed

- Linux: Sandbox mode refuses to start when neither libseccomp nor bubblewrap is available. Landlock alone doesn't
  stop a connection to a Unix socket by its path, so the session bus, `docker.sock` and the SSH agent were
  reachable. System check shows **Other processes: not enforced** in that state. Install `libseccomp2` (present on
  almost every desktop distribution) or bubblewrap. Plugins on such a system don't start either.
- Schedules: a cron line must have five fields (minute, hour, day of month, month, day of week). cronsim also reads
  six fields with seconds first, so `* * * * * *` started a service every second. A six-field schedule saved
  before loses its seconds field when it loads, and runs at most once a minute.
- Project builder: new projects start from Fastify 5.12.8, Hono 4.13.16, @hono/node-server 2.1.6, Elysia 1.4.31,
  Sinatra 4.2 and Puma 8.0.2 or later. The older floors allowed versions with published advisories, one Critical
  (Elysia, prototype pollution).
- Dependency floors raised to the versions CI tests: psutil 7.2 (and below 8, which changes `Process.info`),
  pydantic 2.14, platformdirs 4.12, setuptools 84.1 for building, pytest 9.0.3 for development.
- CI: the test job runs on Ubuntu 24.04 by name, with Ubuntu 26.04 as a job of its own. Releases use
  `actions/attest` (in place of the `attest-build-provenance` wrapper) and `download-artifact` v8, which fails on
  a digest mismatch instead of warning.
- A weekly workflow, **Refresh locks**, compiles every `*.lock` file again with `--upgrade`, checks the result
  with pip-audit, ruff, mypy and the tests, and opens one pull request. Dependabot never read the lock files. It
  needs **Allow GitHub Actions to create and approve pull requests** in the repository's Actions settings. See
  [Dependencies](docs/development.md#dependencies).
- `.env` and `.env.*` files are ignored by git in this repository (`.env.example` is not).

### Fixed

- Project builder: Express 5 apps (Express API, WhatsApp bot) exit with an error when their port is taken. Express 5
  hands the listen error to the callback, which printed "Listening on…" and exited with code 0, so Pythonator saw a
  clean exit.
- Plugins: their messages are read from the pipe in blocks. Reading byte by byte made a 1 MB message take more than
  10 seconds on Windows, which held up a plugin with a big panel and failed the oversized-message test in CI. A
  plugin that sends a message over 1 MB is killed at once by the thread reading it: it was left blocked writing
  the rest of the message, which nobody read, until the background service got round to it.
- Windows: closing the window while Make reachable, or its undo after a removal, was still running no longer raises
  an error in that thread.

### Security

- Windows: Make reachable, its undo, and the firewall-rule check start PowerShell by its full path in System32. A
  bare `powershell.exe` is looked for in the current folder first, which could be a project folder a sandboxed
  service writes, and the planted program would run as administrator after the usual prompt. The window now also
  moves to its data folder and sets `NoDefaultCurrentDirectoryInExePath` at start, as the background service did.
- Git pull: a pull (manual or on start) no longer runs while another service in the same folder, inside it or
  around it, is running, starting or installing in any mode but Full access, nor while one of their console
  commands runs. Such a service could swap `.git/config` between Pythonator's check and git reading it. Pull on
  start skips the pull and says so in the console; a sandboxed service starting in a folder git is pulling waits
  for the pull. The checkout's config is checked again after the fetch, and `credential.helper`, `core.sshCommand`
  and `core.askPass` are pinned on git's command line to your own `~/.gitconfig`. See
  [Git](docs/git.md#hardening).
- Plugins (and so the AI provider and Notify's webhooks) get service output and notes with every value of the
  service's variables and env file masked, whatever its name, plus the password inside a URL. Only secret-looking
  names were masked before, so `DB_PASS`, `DATABASE_URL` or a token in `DISCORD` went through. Numbers, addresses,
  the service's ports and plain settings such as `production` stay readable. The console and the log file are
  unchanged.

## [0.0.17] - 2026-11-07

Bundled plugins: AI assistant 1.3.2.

### Fixed

- AI assistant: an answer that writes a whole file (nothing before `=======`) for a file that exists but is empty,
  such as a new project's `main.py`, is written after you review it. It was refused with *"main.py exists, so say
  which text to replace"*. A whole-file answer for a file the assistant wasn't shown isn't offered, and the chat
  says to add the file with **Add a file…** and ask again.

## [0.0.16] - 2026-11-07

Bundled plugins: Project builder 1.4.0, Notify 1.1.1, AI assistant 1.3.1.

### Added

- Single instance per data folder on Windows, macOS and Linux: starting Pythonator again brings the open window to
  the front, and a start at sign-in exits quietly. Only one background service runs.
- System check: **Left behind by services** lists anything that can keep a port open or a firewall rule in place after
  a service has gone, and **Clean up…** removes it. This covers processes still listening that were started for a
  removed or stopped service (each service's processes now carry `PYTHONATOR_SERVICE`), Pythonator's Docker or Podman
  containers still running for one, and, on Windows, the Make reachable firewall rules, loopback exemption and permit
  of a service that no longer exists. Detection needs no administrator rights. See
  [Environment check](docs/services.md#environment-check).
- Linux: the sandbox launcher is a child subreaper
  ([`PR_SET_CHILD_SUBREAPER`](https://man7.org/linux/man-pages/man2/prctl.2.html)), so Stop also ends processes that
  left the service's process group (`setsid`, double fork).
- Project builder 1.4.0: the setting **Refresh looks for leftover project folders** (off by default) lists unused
  folders in the projects folder, and **Clean up N leftover folders…** deletes the selected ones after confirmation.
  New SDK calls `leftover_projects` and `delete_leftovers`.
- Right-click **Remove…** on a Project builder project offers **Delete project and folder** or **Remove service, keep
  folder**, as the builder's own Delete does. One question covers several selected services.
- Editor: saving over a file that changed on disk since it was opened asks for confirmation.

### Changed

- Service settings: memory and CPU limits moved to a new **Limits** tab. Each limit is a card showing the value and
  its share of the computer, with a slider over the computer's range (fully right means no limit), preset sizes and
  a text box validated as it is typed. Profiles (Small bot, Web app, Worker, No limits) set both at once. See
  [Memory and CPU limits](docs/services.md#memory-and-cpu-limits).
- Service settings: schedules moved from General to a new **Schedule & restarts** tab, alongside the restart rules.
  A service is either **Keep it running** or **Run on a schedule**. A schedule comes from eight presets, a form
  (every day at 09:00, Monday to Friday, weekly or monthly on a given day, …) or text as before; the tab describes it
  in words and lists the next three runs. Restart rules are disabled for scheduled jobs, which are not restarted.
  The saved format is unchanged (`every 15m` or a cron expression). See [Schedules](docs/services.md#schedules).
- **F5** only starts a service; **Ctrl+R** restarts.
- Plugin settings: labels wrap onto as many lines as needed, and yes/no settings are a check box with the label
  beside it.
- AI assistant 1.3.1: edit blocks are parsed in the forms models actually produce (missing `>>>>>>> END`,
  `>>>>>>> REPLACE`, blocks inside code fences, `<<<<<<< SEARCH` under the file name, CRLF). Each change is tried
  against the file before it is offered, tolerating whitespace and indentation differences; a change that would fail
  is not offered, and the chat names the failing edit. One **Review** button per file, from the latest answer.
- Messages to plugins are written by one thread per plugin with a 2,000-message queue; a plugin that falls behind is
  stopped and disabled.
- Typed input to a service is written by its own thread; more than 256 unread lines are refused.
- Container services are prepared and started off the background service's main thread.
- Stats: one process-table pass and one connections call per round for all services, instead of per service
  (previously 17.7 ms per service with 600 processes). `running.json` is written only when the set of running
  services changes.
- Console search highlights matches on screen only, after typing pauses, and counts the rest (previously 353 ms per
  keystroke on a long log). A hidden console polls 4 times a second instead of 60.
- Project builder 1.4.0: takes service state from events instead of reloading all templates and projects on every
  start and stop; template checks run off the main loop with one lookup per tool.
- Opening a file reads only its first 8 KB to detect the encoding.

### Removed

- Unused code: `git.version()` and `MIN_VERSION`, `Supervisor._pids`, `launcher.windows_profile_sid` and
  `windows_acl_sids`, `FileTree.status`, `MainWindow.header`, `ansi.strip`, `PluginHost._write_raw`,
  `NODE_OPTIONS_PRELOAD`.

### Fixed

- Windows: removing a service left its Make reachable firewall rules, loopback exemption and loopback permit in
  place. Nothing listened behind them, but they would admit whatever later ran under the same AppContainer name.
  Removing a service now undoes them (one administrator prompt, only when there is something to undo), as does
  uninstalling.
- A plugin that stopped reading its messages froze the background service.
- Typing into a service that does not read its input blocked the background service (from 131 KB on Linux, about
  4 KB on Windows).
- A slow Docker or Podman (for example, Docker Desktop still starting) stalled all services and the window for up to
  about 90 s.
- Stop or Remove during pull-on-start still launched the service; a double Start created two runners.
- Closing a modified tab with **Save** when the save failed closed the tab and lost the edits.
- A new background service exited while the previous one was shutting down, leaving the window on "connecting".
- Two windows or two background services could run at once, so every question was asked twice and every service ran
  twice.
- Removing a service a second time (a second click or window, while it was stopping or after it had gone)
  force-killed it or showed "No such service".
- Windows: Project builder **Refresh** could offer a leftover folder again while it was being deleted.
- AI assistant: a missing `>>>>>>> END` wrote the next edit block's markers into the file. Pythonator itself now
  refuses any change that adds an edit marker or a hidden-value placeholder.
- Notify 1.1.1: a `Retry-After` header containing a date stopped alert delivery; HTTPS webhooks could redirect to
  HTTP.
- Windows: replacing the private Node.js copy no longer removes one that a running service is still using.
- White ANSI text was invisible on the light theme.
- Header icons are redrawn after a display scaling change.
- A plugin name longer than 64 characters is truncated in API errors.

### Security

- Git pull: a checkout whose `.git/config` holds settings outside an allow-list (`core.sshCommand`, filter drivers,
  `credential.helper`, `include.path` and others) is refused, naming each setting. A sandboxed service could write
  these to run a program outside the sandbox on the next pull. A sandboxed service must now be stopped before a
  manual pull. The empty hooks folder git runs with moved from `/tmp/pythonator-no-hooks` into the private data
  folder. See [Git](docs/git.md#hardening).
- The Python version check before a start reads `pyvenv.cfg` instead of running the project's `.venv` Python with
  `--version` outside the sandbox.
- Sandbox read grants outside the project come only from a real Python installation. Previously, `home` in
  `pyvenv.cfg`, or a symbolic link in `.venv`, could grant read access to any folder.
- Env files are read only when they resolve inside the project folder (a link to `~/.aws/credentials` previously
  loaded the keys). An env file can no longer set `PATH`, `HOME`, `LD_*`, `DYLD_*`, `DOCKER_*`, `BUILDKIT_*`,
  `CONTAINER_*`, `CONTAINERS_*`, `PODMAN_*`, `XDG_*`, `GCONV_PATH`, `LOCPATH`, `PYTHONHOME` or `PYTHONPATH`, which
  reached the sandbox launcher, bubblewrap and the Docker CLI outside the sandbox. Variables set on the service itself
  are unaffected.
- Inherited environment variables named like secrets (`GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `*_API_KEY`) are no
  longer passed to sandboxed services or plugins. Console masking is rebuilt at each start from the variables actually
  passed, including a changed `.env`.
- Sandbox **More folders**: a folder that contains a credentials folder (for example `~/.config`, which holds `gh`
  and `gcloud`) is refused.
- Linux: Landlock-only sandboxes can read only the required device files (no cameras, microphones or `/dev/pts`);
  `/dev/pts` is available only inside bubblewrap. With bubblewrap, the network on, and Landlock below ABI 6 (Linux
  before 6.12), seccomp blocks new Unix sockets, which could reach abstract sockets such as X11's.
- macOS: the Seatbelt profile no longer allows `/dev/ttys*`.
- Compose: named volumes whose `driver_opts` bind a host folder outside the project, and networks named or driven by
  `host`, are refused.
- Container mode refuses a project path containing a comma or quote, which could add a second mount source.
- Plugins: `read_files` and `propose_changes` refuse any path through a symbolic link (a link to `id_rsa` or `.git`
  was followed). An edit touching a hidden value gets the same error as text that is missing or ambiguous, so a value
  cannot be guessed one character at a time. Proposals are refused when they add CI configuration (`.github/`,
  `.gitlab-ci`), npm install scripts, renamed imports (`from os import system as s`), `getattr` with a constructed
  name, or `__builtins__`.
- Plugins: removing a plugin, or installing different code under the same ID, clears its grants and live mode.
  Plugin files that change on disk after verification are treated as new code: disabled, in test mode, with no grants.
- Plugins: rate limits by size as well as count (2 MB/s), 50 stderr lines per second, 2,000 characters per log line,
  and one `hello` per process (a second one re-read the keychain).
- Plugin, program and git text is shown as plain text in labels, message boxes and tooltips. Qt rich text in a
  proposal path (`<img src='/dev/zero'>`) hung the window.
- Windows: a process that cannot be added to its job object is never resumed.
- The background service's token file is written only once it is listening.

## [0.0.15] - 2026-11-06

Bundled plugins: Project builder 1.3.0.

### Added

- Project builder 1.3.0: 38 templates and a **Runtime** filter. New templates: React + TypeScript, Svelte, Solid,
  Preact and Vanilla JS on Vite; Next.js, Angular and Astro; Fastify, Hono and Koa; Litestar, Streamlit and Gradio;
  Go (net/http, Gin), Rust (Axum), Bun (Hono, Elysia), Deno, Java, PHP and Ruby (Sinatra); a Node.js worker.
- Runtime version checks before a start, for every runtime, from the template's requirements and the project's
  `package.json` engines, `requires-python`, `go.mod`, `rust-version` and Composer's `php`. The message names the
  required and installed versions.
- A start reports a listed port that is already in use (and by which program) or, on Windows, reserved for Hyper-V,
  WSL or Docker. A service not listening on its port 30 s after starting gets a note describing what its processes are
  doing.
- Windows: a start reports when a sandboxed server is not yet reachable from the same PC, and **Open** offers **Make
  reachable** first.
- CI starts a Vite app in the Windows sandbox on every push, makes it reachable and loads its page.

### Changed

- The plugin dock is laid out once resizing or moving stops (97 ms to 5 ms per drag step with a long chat). Plugin
  updates are drawn at most every 60 ms, and a hidden tab's only when shown (91 ms to 13 ms per streamed AI chunk).
- The console shows the command that actually runs, including the private Node.js copy in the Windows sandbox.
- Nuxt runs with `--no-fork`, Astro's dev server stays in the foreground, and Astro's starter is generated locally
  instead of being downloaded from GitHub.

### Fixed

- Windows sandbox: Node.js hung at 100% of a core when a program started a child process (Vite, Next.js, Angular, npm
  install scripts). The sandbox's private Node.js copy renames libuv's pipe prefix `\\?\pipe\uv\` to
  `\\?\pipe\LOCAL\`, as libuv 1.53 does. See
  [Node.js programs that start other programs](docs/isolation.md#windows-nodejs-programs-that-start-other-programs).
- Windows sandbox: Vite stopped at *EPERM: operation not permitted, realpath*; the Node.js preload now answers with
  the existing path when Windows refuses `realpath`.
- Linux sandbox: services with listed ports may also bind a system-chosen port (port 0), which Next.js Turbopack and
  Nuxt workers need.
- Rust installed with rustup works in the sandbox (toolchains folder readable, `RUSTUP_HOME` set).
- Nuxt failed with Node.js 22.13; Nuxt 4 needs 22.22.3 or newer, which is now reported before the start.

## [0.0.14] - 2026-11-06

Bundled plugins: Project builder 1.2.1.

### Changed

- Revoking a sandbox's folder permissions after Remove or Delete runs in the background and skips a folder that is
  about to be deleted (previously minutes for a large `node_modules` on Windows).
- The first sandboxed Node.js start copies Node.js in the background, with a console note.
- Project builder 1.2.1: a single **Create** button; an empty name uses the suggested one.

### Fixed

- Windows sandbox: npm and Node.js failed with *EPERM: operation not permitted, lstat 'C:\'* (create-vite, create-vue
  and other Node.js templates). Node.js now starts with `--preserve-symlinks --preserve-symlinks-main` and a preload
  that answers for folders above the sandbox's own.
- Windows: deleting a project now deletes its folder. Open files are closed and the folder unwatched first, paths
  over 260 characters are handled, and files briefly locked by a virus scanner are retried.
- Windows CI tests the **Let sandboxes use it** check as it has worked since 0.0.13.

## [0.0.13] - 2026-11-06

Bundled plugins: Notify 1.1.0, Project builder 1.2.0, AI assistant 1.3.0. Plugin API 5.

### Added

- Plugin API 5: views (`ui.tabs`) and icon buttons (`ui.button(..., icon="refresh")`).
- Notify 1.1.0: **Services** and **Events** views. Project builder 1.2.0: **New project** and **Projects** views.
- Header **Details (i)**: process, CPU, memory, ports, isolation, restart policy and folder; selecting a line copies
  it. Also available from the service list's context menu.

### Changed

- Plugin panels cannot exceed the dock: text and paths wrap, buttons are elided with "…", drop-downs fill the row, and
  the dock is at least 240 px wide.
- The header is a single line of icon buttons (Start, Stop, Restart, Packages, Configure, Details, More).
- The editor no longer has its own toolbar (New, Open and Save sit beside its tabs); tabs are lower, and the console
  uses icon buttons.

### Fixed

- Windows sandbox: Node.js projects failed with "Access is denied" when Node.js was installed for all users in
  `C:\Program Files\nodejs` (the 0.0.12 check never triggered). Sandboxes now run a private Node.js copy, made once
  per version in the data folder, and npm runs without a batch file.
- AI assistant: edits for the prompt's example path (`path/to/file.py`) or the wrong folder go to the one file that
  contains the replaced text; trailing spaces are ignored when matching; an edit that matches no file is dropped with
  a note instead of creating a new file.

## [0.0.12] - 2026-11-06

### Added

- Project builder: Discord, Telegram and WhatsApp bots in Python or Node.js (discord.py, discord.js,
  python-telegram-bot, grammY, and a signature-checked WhatsApp Cloud API webhook), an Express API and a Python
  worker; **↻ Refresh**, a project list, and Delete (only for folders the builder created, after confirmation).
- Windows: **System check** offers **Let sandboxes use it…** for a runtime folder that sandboxes cannot read; one
  administrator prompt grants read and execute.
- The CPU and RAM meter shows Pythonator's own share (window, background service, plugins and services).

### Changed

- Plugin panels are tabs in a single Plugins dock again, with no separate windows or layout lock.
- Panel rows of cards, lists or buttons wrap; wide tables scroll horizontally.
- Templates: Django 5.2 LTS (5.2.19 or newer), Flask 3.1.3 or newer, FastAPI 0.162 or newer.

### Fixed

- The console stuttered while the window was resized or moved (since 0.0.11).
- AI answers were cut off at the bottom of their bubble, and the note under a question overlapped it.
- Windows: Node.js in `C:\Program Files\nodejs` without the sandbox read entry failed with "Access is denied"; the
  start now reports it.
- The Plugins window stopped long permission descriptions and setting names from wrapping.
- Docker Compose on Windows: Docker Desktop's Compose plugin was not found; an old or missing Compose is now reported.

## [0.0.11] - 2026-11-06

Bundled plugins: Notify 1.0.0, Project builder 1.0.0, AI assistant 1.2.0. Plugin API 4.

### Added

- Notify plugin, replacing Health checks and Webhooks (settings migrate automatically): a single timeline of crashes,
  abandoned restarts, blocked starts, failed installs, failing health checks, recoveries and anomalies (growing
  memory, CPU stuck high, crash loops, error bursts, a lost port). Alerts appear in the window, as tray notifications,
  and on Discord, Slack, Telegram, Teams, ntfy or any URL. Services can be muted for a period.
- Project builder plugin: React, Vue, Nuxt, FastAPI, Flask or Django projects created by each framework's own tool in
  the new service's sandbox, added on a free local port. New plugin permission `create_services` (plugin API 4).
- Docker Compose projects as a single service in Container mode: `compose up`, `compose stop`, and **Compose ▾** for
  build, pull, ps and down. The resolved file is checked before each start and build; privileged containers, host
  namespaces, devices, mounts outside the folder, the engine socket and ports open to all networks are refused.
- Plugin panels as docks, tabbed or floating, with a remembered layout.
- Service window: **＋ Add** and **− Delete** in the Environment and Folders tables, and **Import from env file**.
- **System check** redesigned: system, current load, installed runtimes and engines with versions, and enforced
  sandbox protections. **Copy report** omits computer and account names.

### Changed

- AI assistant 1.2.0: complete answers rendered as Markdown (plugin API 4). Plugin messages may be up to 1 MB.
- Panel text no longer re-wraps on every pixel of a resize; plugin tables update in place; the console does not
  redraw while the window is dragged or resized.

### Fixed

- Editor: saving added blank lines to CRLF files; pasted text now keeps the file's line endings; Undo straight after
  opening emptied the file. Non-UTF-8 files open read-only with a note.
- Windows: a plugin showed "Unhandled exception in script" (`BrokenPipeError`) when closed or restarted.

## [0.0.10] - 2026-11-05

Bundled plugins: AI assistant 1.1.0. Plugin API 3.

### Added

- Editor find bar: match counter, highlighting of all matches, match case, whole word and regular expressions, and a
  replace row (Replace all is a single undo step). Ctrl+F, Ctrl+H, Enter/Shift+Enter, F3, Esc.
- AI assistant 1.1.0: chat view with streamed answers, code blocks and concise errors; a **Model** list per service,
  fetched, cached for a day and refreshed with **↻ Refresh**, which also tests the connection.
- Plugin API 3: chat panels (`ui.chat`, `ui.message`).

### Fixed

- AI assistant: a misspelt model name is reported instead of failing with a 404; Gemini errors are readable.

## [0.0.9] - 2026-11-05

### Fixed

- Windows: uninstalling ends every Pythonator process still running from the install folder, so `Pythonator.exe` can
  always be deleted. The installer test lists leftover processes and shows the uninstall log.

## [0.0.8] - 2026-11-05

### Added

- Console input switch **stdin / shell** (Ctrl+\`): shell mode runs a command in the service's folder, sandbox or
  container, with its environment first on `PATH`, while the service keeps running.

### Changed

- Custom start commands run without a shell and resolve their program from `.venv` and `node_modules/.bin` before
  `PATH`. Python, Node.js and custom services get the same `PATH`; Pythonator's own venv no longer leaks into them.
- Selecting a service shows its log scrolled to the bottom.

### Fixed

- Windows: Full access Python services exited immediately with code 106. The venv's `python.exe` launcher is now
  skipped and its exit codes are explained.
- Plugins read Pythonator's messages as UTF-8 on every Windows code page ("surrogates not allowed" in the AI
  assistant).
- Health checks use the port a server actually listens on.
- Guard names its whole-computer rule correctly.

## [0.0.7] - 2026-11-05

### Fixed

- Windows: starting, stopping or restarting a service showed the busy mouse pointer. Every process Pythonator starts
  now disables "app starting" feedback.

## [0.0.6] - 2026-11-05

### Added

- **Settings**: start services one at a time (default), a few at a time, or all at once; log rotation size and count;
  keep running in the tray when the window is closed; start at sign-in, in the tray.
- Service list: multiple selection with Ctrl or Shift, reordering with ↑ ↓ or Alt+↑ / Alt+↓, and starting, stopping
  or removing several services from the context menu.

### Removed

- The separate start-order window.

### Fixed

- Two services in the same folder created its `.venv` at the same time.

## [0.0.5] - 2026-11-05

### Added

- Start queue for **Start all** and sign-in: list order, 4 at a time by default (**Settings → Start at once**).
- **Settings**: start order and removing several services.

### Changed

- A first start skips setting up pip when the project has nothing to install.

## [0.0.4] - 2026-11-05

### Changed

- The console spreads output over every frame (60 per second, 30 under a flood); the "skipped" note appears at most
  once a second.
- Output is read in chunks instead of line by line (from 84% to 7% of a core in a flood test), and the console caps
  what a single frame draws.

## [0.0.3] - 2026-11-05

### Fixed

- Sandbox mode: set-up steps could not write the project's `.venv`, which was also bound read-only. Own environments
  are now created and populated on every system. On Windows, pip is set up as a separate step.

## [0.0.2] - 2026-11-05

### Added

- **Own environment** per service, on by default: Start creates `.venv`, `node_modules` or `vendor` when missing;
  Deno, Go, .NET and Rust get a private package cache. **Packages ▾ → Recreate environment** starts it afresh.
- **Start all** and sign-in start services one after another.

### Fixed

- The editor stalled while a service flooded its console. The console now draws for a few milliseconds per frame,
  pauses during typing and scrolling, thins floods (the log keeps everything) and handles carriage returns.
- Plugin panel redraws took keyboard focus from the editor.

## [0.0.1] - 2026-11-05

First release from the relaunched repository. Services and settings from the preview builds carry over. Plugin API 2.

### Added

- Services with restarts and back-off, schedules (intervals or cron), memory and CPU limits for the whole process
  tree, live CPU, memory and ports, and rotating logs with secrets masked. Services keep running when the window is
  closed.
- Sandbox mode by default (Landlock, seccomp and bubblewrap on Linux; Seatbelt on macOS; AppContainer and a job object
  on Windows), Container mode with hardened Docker or Podman, and Full access after confirmation.
- Per-service ports, local only or network-wide, and **Make reachable** on Windows.
- Python, Node.js, Bun, Deno, Go, Rust, Java, .NET, Ruby, PHP, PowerShell, shell scripts or any command.
- Sandboxed plugins: Webhooks, Guard, Health checks and the AI assistant.
- Built-in editor, Git clone and pull, and installers for Windows, macOS and Linux (x64 and arm64).

## Preview builds

Released before the relaunch as 0.0.1 to 0.0.11. The downloads have been withdrawn; the notes are kept for reference.

### Preview 0.0.11

#### Added

- Per-service **CPU limit** in percent of one core for the whole process tree: a hard cap on the job object on
  Windows, `--cpus` for containers, and on Linux and macOS the tree is paused in short slices while over the limit,
  measured twice a second.
- The console lists a service's limits and how they are enforced at each start; the header shows CPU use against the
  limit.
- Guard: when the computer stays above 95% CPU or 92% memory for 30 s, the service using the most receives Guard's
  action, if it uses at least a quarter of all cores or a tenth of memory. Both thresholds are configurable.
- AI assistant, rebuilt as a sandboxed official plugin (off by default): Claude, OpenAI, Gemini, DeepSeek, Ollama or
  any OpenAI-compatible server. Suggested changes open a review window; nothing is written without Apply.
- Plugin API 2: text box, drop-down and code block panels; `read_files` (read-only, secrets hidden) and
  `propose_changes` permissions. Changes to scripts, emptying files, and code that runs shell commands, deletes files
  or evaluates strings are refused.
- `inspect` plugins receive the computer's CPU and memory (`self.system`) and each service's `cpu_limit`.
- **Help ▾ → About Pythonator** with credits, support link and versions; **Help ▾** opens the documentation.

#### Changed

- Plugin `secret` settings are stored in the system keychain (Windows Credential Manager, macOS Keychain, Secret
  Service) rather than `settings.json`; existing secrets move on their next save.

### Preview 0.0.10

A rewrite of the engine under 0.0.9's window: about 10,000 lines instead of 24,000, type-checked with
`mypy --strict`.

#### Added

- A background service runs services independently of the window; a crash of either leaves services running, and
  leftovers are cleaned up.
- Restart back-off from 1 s to 60 s with a limit per 10 minutes; schedules with intervals, `@daily` or cron; a memory
  limit for the whole process tree.
- Package installs inside the service's isolation (`.venv`, `node_modules`, `vendor`). Rust and PowerShell runtimes.
- Environment check before each start; **System check** for the sandbox protections enforced.
- Isolation modes **Sandbox** (default), **Container** and **Full access**; a service does not start if a protection
  cannot be applied. Linux: Landlock (files, TCP ports, scopes), seccomp and bubblewrap. macOS: Seatbelt. Windows:
  AppContainer with recorded, reversible folder grants, and a job object.
- Ports per service, local only or other computers too. Windows **Make reachable**: loopback exemption, loopback permit
  and firewall rules for that service only; **Undo** removes them.
- Containers with all capabilities dropped, `no-new-privileges`, a read-only image, a process limit, and optional
  container networks.
- Plugins in sandboxed processes with a validated protocol; official plugins verified by SHA-256 against a reviewed
  catalogue; third-party plugins start in test mode with no permissions and are disabled on floods, invalid messages
  or repeated crashes. Permissions `network`, `read_logs`, `inspect`, `control`, `policy`, `ui`. Official plugins
  Webhooks, Guard and Health checks.
- QScintilla editor; hardened Git clone and fast-forward-only pull; documented API with `docs/examples/pyctl.py`.
- Installers: Windows `setup.exe` and `.zip`, macOS `.dmg` (Apple silicon and Intel), Linux `.deb` (x64, arm64) and
  `.tar.gz`. Updates stop the background service first; uninstalling on Windows removes sandbox folder permissions.
- CI on Windows, macOS and Linux with installer tests; `pip-audit` on hash-pinned lock files; attested releases.

#### Changed

- The console draws for at most 10 ms per frame and skips what does not fit (with a note; the log keeps everything);
  only the displayed service's output reaches the window; fast writers block on their pipe instead of building a
  backlog.
- One editor zoom level for all files (Ctrl+, Ctrl-, Ctrl+=, Ctrl+wheel, Ctrl+0 resets), remembered.
- Start all and Stop all return immediately; a second Stop kills whatever is still stopping.

#### Removed

- The AI assistant plugin, plugin `pyproject.toml` packaging, `bot.json` and other old-format migrations, the in-app
  API reference generator, and the code signing scripts. 0.0.9's `pythonator.json` is still imported once.

### Preview 0.0.9

#### Added

- Windows sandbox: **Make reachable…** adds a firewall rule admitting this PC only, so a sandboxed server answers on
  `http://127.0.0.1:<port>/`. Services set up with 0.0.8 or earlier need it once.
- Dependencies menu for Python: **Create venv**, **Recreate venv…** and a **Use .venv** switch; other runtimes show
  where their packages go.

#### Changed

- A created venv is used from then on, even if the service was set to another Python.

### Preview 0.0.8

#### Added

- Python services ask before running outside the project's venv (none, unused or broken), with a remembered choice per
  service.
- `$` commands that install account-wide (`npm -g`, `yarn global`, `gem install`, `composer global`, `pip --user`)
  ask first and offer the project-local command.

#### Changed

- After creating or recreating the venv, the requested start, install or command runs in it.

### Preview 0.0.7

#### Fixed

- Windows sandbox: "working folder read only" was not enforced for services previously set to "read & write"; writes
  allowed by another folder's permissions are now blocked too.

### Preview 0.0.6

#### Removed

- The command line added in 0.0.5.

### Preview 0.0.5

#### Added

- Dependency vulnerability checks on every change and weekly; Dependabot for the app's and official plugins'
  requirements.

#### Fixed

- Windows sandbox: temporary files go to the sandbox's own temporary folder instead of the service's folder.

### Preview 0.0.4

#### Fixed

- Windows sandbox: first attempt at keeping temporary files out of the service's folder (completed in 0.0.5).

### Preview 0.0.3

#### Added

- Guard plugin rewritten as a security policy: 19 rules for isolation, exposed ports, outgoing connections, container
  images and a resource watchdog; each can warn, stop or restart, with per-service exceptions.
- Webhooks plugin: Discord, Telegram, WhatsApp, Slack, Teams, ntfy, Pushover, Gotify or a custom endpoint, batched per
  service and rate-limited.
- Plugin SDK 2: service set-up and resource use; multi-line and tabbed settings.
- Outgoing connections are recorded per service alongside its listening ports.

#### Fixed

- Windows sandbox: pip installs and Python's `tempfile` work.

### Preview 0.0.2

#### Fixed

- Windows sandbox: **Create venv** works and the venv always has pip; **Install dependencies** adds pip to a venv
  without it; `$` commands run in cmd (PowerShell cannot run in the sandbox); services no longer inherit Pythonator's
  DLL search path or internal variables.

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
- Plugins: official ones verified against a reviewed catalogue; third-party ones always sandboxed. Official plugins:
  Webhooks, Guard and the AI assistant (Claude, OpenAI, Gemini, DeepSeek, Ollama).
- A website with guides, a glossary, a dependency list and an API reference.

[Unreleased]: https://github.com/cfunkz/Pythonator/compare/v0.0.19...HEAD
[0.0.19]: https://github.com/cfunkz/Pythonator/compare/v0.0.18...v0.0.19
[0.0.18]: https://github.com/cfunkz/Pythonator/compare/v0.0.17...v0.0.18
[0.0.17]: https://github.com/cfunkz/Pythonator/compare/v0.0.16...v0.0.17
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
