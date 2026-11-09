<p align="center">
  <img src="pythonator/resources/icon-256.png" alt="" width="96" height="96">
</p>

<h1 align="center">Pythonator</h1>

<p align="center">
  A service manager for your own computer: web apps, APIs, workers, databases and scheduled jobs, each one in a jail it can't leave.<br>
  <a href="docs/getting-started.md">Getting started</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="screenshots/main.png" alt="The main window: services on the left, a live console, and the security stack that protects the selected service" width="900">
</p>

Pythonator keeps a set of services running on Windows, macOS and Linux. A background service (`pythonatord`) makes
what runs match what you asked for, and nothing runs until you have seen and approved what it may reach.

## What it does

* **Every service runs in a jail.** Four modes, the same on every OS where the OS allows it:
  * **Sandbox** (the default): bubblewrap, Landlock and seccomp on Linux, a Seatbelt profile on macOS, an
    AppContainer in a job object on Windows. The service sees its own folder and its runtime, nothing else of
    your home: not your SSH keys, browser data or other projects.
  * **Container**: Podman or Docker with a fixed, hardened set of flags and a network of its own.
  * **WSL** (Windows): the Linux sandbox inside Pythonator's own Debian, with no drives and no Windows interop.
  * **Full access**: runs as you, and every place that shows the service says so.
* **You approve a plan, not a checkbox.** Each change produces a plan: which layers protect the service on *this*
  machine, and what would widen (a folder, a host, a port). Your approval is bound to a hash of those fields, so
  nothing that widens access slips in later without asking again.
* **Refused, never "cooperative".** If a layer can't be enforced here, the start is refused with a code and the
  fix. A layer that is weaker on this machine is shown as *reduced*, in the plan and on the first console line.
* **It keeps them running.** Restarts with back-off, health checks, schedules, kernel limits, and a cool-down retry
  after giving up. Closing the window never stops a service.
* **Traffic you can read.** Traffic out is Off, a list of hosts (through Pythonator's egress gate) or Anywhere. Each
  blocked connection is listed with one click to allow it.
* **Logs that can't freeze the window.** Output is stored in indexed segments and read by range. Secrets, token
  formats and private keys are masked before anything reaches the disk, a plugin or a webhook.
* **Plugins that can't hurt you.** Each plugin runs in its own jail with no network and no home, starts with no
  permissions, and its grants are tied to a hash of its code. Guard, Notify, AI assistant and Builder ship with it.

## Install

Download the file for your system from the [latest release](https://github.com/cfunkz/Pythonator/releases/latest).
Each carries its own Python, so there is nothing else to install.

| System | Download |
| --- | --- |
| Windows 10 or 11 | `Pythonator-<version>-windows-x64-setup.exe` (for your user, no administrator rights), or the portable `.zip` |
| macOS 14 or newer | `Pythonator-<version>-macos-arm64.dmg` (Apple silicon) or `-macos-x64.dmg` (Intel): drag it to Applications |
| Debian, Ubuntu | `sudo apt install ./pythonator_<version>_amd64.deb` (or `_arm64.deb`); it brings bubblewrap and passt for Sandbox mode |
| Any other Linux | `Pythonator-<version>-linux-x64.tar.gz` (or `-arm64`): unpack and run `./Pythonator/pythonator` |

The background service starts at login, as you. To remove Pythonator: Settings → Apps on Windows, `sudo apt remove
pythonator` for the .deb, or `pythonatorctl uninstall` (see [Command line](docs/cli.md#uninstall)). The installers
are built, tested and attested by the release workflow from `packaging/`.

Then open Pythonator, click **New service**, pick a folder and approve the plan. See
[Getting started](docs/getting-started.md).

## Documentation

| | |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install on each OS, first service, approve, start |
| [Services](docs/services.md) | Modes, runtimes, restarts, health, schedules, limits, logs, secrets |
| [Isolation](docs/isolation.md) | What each layer does per OS, and the known limitations |
| [Networking](docs/networking.md) | Traffic out, the egress gate, ports in |
| [Plugins](docs/plugins.md) | Guard, Notify, AI assistant, Builder, permissions, writing your own |
| [Command line](docs/cli.md) | Version and uninstall |
| [System check](docs/system-check.md) | Probes, speed on this machine, fixes |
| [Security model](docs/security.md) | What is defended, how, and what isn't |
| [Security cases](docs/security-cases.md) | All 286 cases from earlier audits, answered |
| [Earlier fixes](docs/earlier-fixes.md) | Every fix from the old app, and where it is now |
| [Architecture](docs/architecture.md) | Daemon, reconciler, policy compiler, ledger |
| [Development](docs/development.md) | Tests, golden files, CI, release checklist |

## Licence

[GPL-3.0-or-later](LICENSE).
