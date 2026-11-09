# Getting started

## 1. Install

Download the file for your system from the [latest release](https://github.com/cfunkz/Pythonator/releases/latest)
or the [website](https://cfunkz.github.io/Pythonator/#download). Each one carries its own Python, with every package
checked against the hashes in `requirements.lock` when it was built, so you don't need Python or anything else.

| System | Download | Install |
| --- | --- | --- |
| Windows 10 or 11 | `Pythonator-<version>-windows-x64-setup.exe` | Run it. It installs for your user only (no administrator rights), adds a Start menu entry, puts `pythonatorctl` on your PATH and lists Pythonator in Settings → Apps. |
| | `Pythonator-<version>-windows-x64.zip` | Portable: unzip it anywhere and double-click `Pythonator.cmd`. |
| macOS 14 or newer | `Pythonator-<version>-macos-arm64.dmg` (Apple silicon) or `-macos-x64.dmg` (Intel) | Open it and drag Pythonator to Applications. It isn't signed or notarized yet (see [1.17.6](security-cases.md)), so macOS asks before the first start; check the download against `SHA256SUMS` and its attestation first (below). |
| Debian, Ubuntu | `pythonator_<version>_amd64.deb` or `_arm64.deb` | `sudo apt install ./pythonator_<version>_amd64.deb`. It goes to `/opt/pythonator`, adds `pythonator` and `pythonatorctl`, an app menu entry, and also installs bubblewrap and passt for Sandbox mode. |
| Any other Linux | `Pythonator-<version>-linux-x64.tar.gz` or `-linux-arm64.tar.gz` | Unpack it anywhere and run `./Pythonator/pythonator`. To start the background service at login: `./Pythonator/pythonatorctl service install`. |

The background service `pythonatord` starts at login, as you: a logon task on Windows, a LaunchAgent on macOS
(set up the first time you open the app), a systemd user unit on Linux. If it isn't running, the window starts it.

To update, install the new version over the old one: the installer stops the running version first. To remove it,
see [Command line](cli.md#uninstall). Your service folders are never touched.

| | Installed in | Your settings and logs |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\Programs\Pythonator` | `%LOCALAPPDATA%\Pythonator` |
| macOS | `/Applications/Pythonator.app` | `~/Library/Application Support/Pythonator` |
| Linux | `/opt/pythonator` (.deb) or where you unpacked it | `~/.local/share/Pythonator` |

Each download is attested by the release workflow. To check one: `gh attestation verify <file> --repo
cfunkz/Pythonator`, or compare it with `SHA256SUMS` on the release page. Windows SmartScreen and macOS Gatekeeper
warn about unsigned downloads; Pythonator never asks you to turn either off.

To run from source instead (Python 3.12 or newer), see [Development](development.md#set-up).

### What each OS needs for Sandbox mode

* **Linux**: `bubblewrap`, and `passt` for any network. The .deb installs them; otherwise install them yourself, for
  example `sudo dnf install bubblewrap passt`. Kernel 5.13 or newer adds Landlock. For CPU and task
  limits on a server you reach over SSH, run `loginctl enable-linger $USER` once.
* **macOS**: nothing; Seatbelt is part of the OS.
* **Windows**: nothing; AppContainer and job objects are part of the OS. For WSL mode, also install WSL
  (`wsl --install`) and then click **Set up** under Engines → WSL.

Container mode needs Podman or Docker on any OS.

## 2. Check this machine

Open Pythonator and click the shield on the left (**System check**).

Each layer shows ✓, *reduced* or missing for this machine, with the fix. Speed (start time, memory) is measured
here and kept for 30 days.

## 3. Add a service

A service is anything that runs from a folder: an API, a web app, a worker, a database, a scheduled job. Click
**New service** (Ctrl+N):

1. **Folder**: for example `~/services/api`. Pythonator suggests a name, the runtime and the command (`main.py`,
   `app.py`, `server.js`, `go.mod`, `Cargo.toml` and so on).
2. **Mode**: keep **Sandbox** unless you have a reason not to. See [Services](services.md#modes).
3. **Traffic out**: *Off* (the default), *Only these hosts* (through the egress gate) or *Anywhere*.
4. **Ports in**: closed unless it serves something; open to this computer or to your local network.
5. **Review plan**.

Some folders are refused in every mode, with a code and the reason: your whole home folder, a system folder,
`~/.ssh` and other folders that hold keys, and Pythonator's own data folder. See [Isolation](isolation.md#folders).

## 4. Approve the plan

The plan shows the isolated environment this service becomes on this computer, from the outside in, and each layer that is weaker
here than it could be. **Approve and start** runs it.

Your approval is bound to a hash of everything that could widen access: mode, folders, traffic, hosts, ports,
limits, secrets and the image. Change any of them and it asks again. A new command, restart setting or schedule
doesn't ask, because the isolated environment around it stays the same.

## 5. Watch it

* The **console** shows the output, read by range so a noisy service never slows the window.
* The **security stack** on the right shows what protects it now.
* **Resources** shows CPU, memory and tasks against the limits.
* If it crashes, it restarts after 2 s, 4 s, 8 s… and gives up after 5 crashes in a row, says why, and tries again
  after a 10-minute cool-down.

Close the window and the service keeps running.

Next: [Services](services.md) · [Networking](networking.md) · [Plugins](plugins.md)
