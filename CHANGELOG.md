# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
Dates are UTC.

## [0.0.1] · 2026-11-09

The first release of the rewrite: a new codebase around a background service, a policy compiler and approved
plans, on Windows, macOS and Linux. Every fix from the old app (0.0.1 to 0.0.19) has a verdict in
[docs/earlier-fixes.md](docs/earlier-fixes.md), and all 286 security cases from its audits are answered in
[docs/security-cases.md](docs/security-cases.md).

### Added

- **Long jobs never hold up the window**: the background service answers each request on its own, so WSL setup, an
  image pull or a Python install runs while you open files and start or stop services. The status bar shows the job
  and its latest step; asking for the same job twice says it's already running.
- **Console colours**: ANSI colours and bold are shown as colours. Other escape sequences are dropped, control
  characters show as visible marks (␇, ␛), bidi overrides can't reorder a line, and output is decoded as UTF-8 even
  when a character is split across an over-long line (which used to be dropped).
- **WSL limits**: the runner turns on the memory, CPU and tasks controllers for its own `pythonator/` cgroup in
  Pythonator's distribution (WSL's fresh hierarchy has them off, so writing a limit said "Permission denied"). An
  updated runner is copied into an existing distribution automatically. A service that can never run (a layer
  refused, the engine refused, no such program) stays Blocked until you start or change it, instead of restarting.

- **Engines → Services on engines** lists WSL services in the same table as containers (engine WSL, state, owner);
  they run as processes in the distro, so they never appeared there before. The window opens a little smaller.
- **Service list**: rename a service (⋯ → Rename; its log and approval move with it), move services up or down
  (⋯ → Move up/down, or Alt+Up/Down; the order is saved), and pick several group chips to show those groups together.
- **Website**: the GitHub Pages site (landing page and docs) is rewritten for this version.
- **Tray icon**: shows how many services run, with Open, Start all, Stop all and Quit. Closing the window hides it
  to the tray; services keep running either way.
- **App settings** (the wrench at the top): start services at login, open the window at login in the tray, keep
  it in the tray when closed, a tray message when a service stops working, and Start all / Stop all.
- **Editor**: completion from the project's own modules (module.name, Class.method) and the packages in its
  environment, read by the background service with `ast` and never imported or run; tabs you can drag to reorder;
  each tab keeps its own undo and redo, and each service's tabs stay open while the window runs; the Problems
  panel shows one problem until you open it.
- **GUI apps**: a service may be a desktop app (PyQt, Tk, Electron…). Full access on every OS, and Sandbox on
  Windows, where the job's UI limits keep other windows and the clipboard out of reach. The Fit check spots one.

- **Background service `pythonatord`** owns every service and keeps running when the window closes. The window is a client
  over a Unix socket in an owner-only folder (loopback TCP on Windows) with mutual HMAC
  challenge-response; the key never crosses the connection.
- **Four modes**, compiled per OS by one pure function into the exact command and a plan of layers:
  - **Sandbox**: bubblewrap, Landlock and libseccomp on Linux; Seatbelt on macOS; AppContainer plus a job object
    on Windows.
  - **Container**: Podman or Docker with `--read-only`, `--cap-drop ALL`, `no-new-privileges`, limits, your user
    id and a network per service.
  - **WSL** (Windows): the Linux sandbox inside Pythonator's own Debian, with no drives and no interop. Set it up
    from Engines → WSL → Set up.
  - **Full access**: runs as you, labelled as not enforced.
- **Plans and approval**: widenings are listed in words, and your approval is bound to a SHA-256 risk hash.
- **Egress gate**: traffic to allowed hosts only, with each blocked connection listed and one click to allow it.
- **Ownership ledger** for processes, containers, networks, images and Windows folder grants: written before each
  action, finished or undone after a crash. Nothing Pythonator didn't create is touched.
- **Supervision**: restarts with back-off, give-up with a 10-minute cool-down retry, HTTP and TCP health checks,
  cron schedules with catch-up, kernel limits (cgroup, job object, container) with a labelled watchdog fallback.
- **Log store** with indexed segments (64 MB per service), range reads, and masking before write: secret-looking
  names, URL passwords, known token formats (GitHub, GitLab, Slack, AWS, Google, Stripe, `sk-` keys, JWTs) and PEM
  private keys.
- **Editor** with ruff problems (ruff runs isolated), private git snapshots in the data folder, and Try in a copy.
- **System check**: probes per OS, speed measured on this machine for 30 days, and fixes. The protection tables in
  [docs/isolation.md](docs/isolation.md) are generated from the same data.
- **Plugins** in their own isolated environments with no permissions until granted, grants tied to a hash of the code, and HTTPS
  through the daemon to granted hosts only. Bundled: **Guard** (rules before every approval), **Notify** (Slack,
  Discord, ntfy or any webhook), **AI assistant** (reviewed edits only) and **Builder** (new services from
  templates). Official plugins are pinned by hash in `official.json`.
- **Downloads for each OS**, each with its own Python and every package checked against `requirements.lock` hashes:
  Windows `setup.exe` (for your user, no administrator rights, listed in Settings → Apps) and a portable `.zip`;
  macOS `.dmg` and `.zip` for Apple silicon and Intel; Linux `.deb` and `.tar.gz` for x64 and arm64. Built,
  installed, tested and attested by the release workflow.
- **`pythonatorctl`**, kept small because Pythonator is used from its window: `version` and `uninstall`
  (`--remove-data`). Uninstall takes back every folder permission first, then removes the login service, the
  shortcuts and the install folder.
- **Folder checks follow links.** A service folder, an extra folder or a new project folder is checked as written and
  where its links lead, against your home folder's real location, so a link to `~/.ssh` is refused even when your
  home is reached through a link (`/home` → `/data/home`, `/var` → `/private/var`).
- **Line endings** are LF on every OS (`.gitattributes`), so the pinned hashes of the bundled plugins match on Windows.
- **CI** on Linux (with and without bubblewrap), macOS and Windows; pip-audit weekly; image checks; a gated,
  attested release with SHA256SUMS.

### Known limitations

See [docs/isolation.md](docs/isolation.md#known-limitations) and the open items at the top of
[docs/security-cases.md](docs/security-cases.md). In short: macOS limits are a watchdog and a child that clears its
environment and starts a new session can outlive Stop; Windows doesn't check the data folder's DACL; Node.js under
the Windows AppContainer isn't verified on a real machine yet; releases aren't code-signed.
