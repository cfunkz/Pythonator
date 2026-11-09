<p align="center">
  <img src="pythonator/resources/icon-256.png" alt="" width="96" height="96">
</p>

<h1 align="center">Pythonator</h1>

<p align="center">
  A service manager for your own computer: web applications, APIs, workers, databases and scheduled jobs, each
  running in its own isolated environment.<br>
  <a href="docs/getting-started.md">Getting started</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="screenshots/main.png" alt="The main window: services on the left, a live console, and the isolation layers that protect the selected service" width="900">
</p>

Pythonator keeps a set of services running on Windows, macOS and Linux. A background service (`pythonatord`)
reconciles what is running with what you configured, and no service starts until you have reviewed and approved
the access it is given.

## Features

* **Isolation for every service.** Four modes, consistent across operating systems where the platform allows it:
  * **Sandbox** (default): bubblewrap, Landlock and seccomp on Linux, a Seatbelt profile on macOS, and an
    AppContainer within a job object on Windows. The service can access its own folder and its runtime only; your
    SSH keys, browser data and other projects are not visible to it.
  * **Container**: Podman or Docker with a fixed, hardened set of options and a dedicated network.
  * **WSL** (Windows): the Linux sandbox inside Pythonator's own Debian distribution, with no Windows drives
    mounted and Windows interoperability disabled.
  * **Full access**: runs with your user's permissions, and is labelled as such wherever the service is shown.
* **Explicit approval.** Each change produces a plan that lists the protections in effect on this machine and any
  access that would widen (a folder, a host, a port). Approval is bound to a hash of those fields, so any later
  change that widens access requires approval again.
* **Strict enforcement.** If a protection cannot be enforced on this machine, the service is not started, and the
  reason and remedy are shown. A protection that is weaker on this machine is marked *reduced* in the plan and in
  the first line of the console.
* **Supervision.** Restarts with back-off, health checks, schedules, kernel-enforced resource limits, and a delayed
  retry after repeated failures. Closing the window never stops a service.
* **Outbound traffic control.** Outbound traffic is Off, limited to a list of hosts (through Pythonator's egress
  gate), or unrestricted. Each blocked connection is listed and can be allowed with one click.
* **Efficient, masked logs.** Output is stored in indexed segments and read by range, so large logs do not slow the
  interface. Secrets, token formats and private keys are masked before they reach the disk, a plugin or a webhook.
* **Isolated plugins.** Each plugin runs in its own isolated environment with no network access and no access to
  your home folder. Plugins start with no permissions, and every grant is tied to a hash of the plugin's code.
  Guard, Notify, AI assistant and Builder are included.

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
