# Getting started

## Install

**From a release.** Download the file for your system from the
[latest release](https://github.com/cfunkz/Pythonator/releases/latest):

| System | File |
| --- | --- |
| Windows 10/11 | `Pythonator-<version>-windows-x64-setup.exe`, the installer (or the portable `.zip`) |
| macOS | `Pythonator-<version>-macos-arm64.dmg` (Apple silicon) or `-macos-x86_64.dmg` (Intel): drag it to Applications |
| Ubuntu, Debian, Mint | `pythonator_<version>_amd64.deb`: `sudo apt install ./pythonator_<version>_amd64.deb` |
| Raspberry Pi OS, ARM Linux | `pythonator_<version>_arm64.deb` |
| Other Linux | `Pythonator-<version>-linux-<arch>.tar.gz`: unpack and run `Pythonator/Pythonator` |

The files aren't code signed. On Windows, SmartScreen may say "Windows protected your PC": click **More info**,
then **Run anyway**. On macOS, open the app once, then **System Settings > Privacy & Security > Open Anyway**.

Updating installs over the old version and stops the background service first. Uninstalling (Windows: *Apps* in
Settings) also takes the sandboxes' entries off your folders' permissions; your settings and logs stay.

**From source.** You need Python 3.12 or later. The lock file pins every dependency with its hash, so you get
exactly the versions that were tested and audited:

```bash
git clone https://github.com/cfunkz/Pythonator.git
cd Pythonator
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
pythonator
```

`pip install -e .` on its own works too and picks the newest allowed versions.

On Linux, install **bubblewrap** (`sudo apt install bubblewrap`, `sudo dnf install bubblewrap`) for the strongest
sandbox. Without it the sandbox still works with Landlock and seccomp, which every current kernel has.
**System check** in the toolbar shows what your computer enforces.

## Your first service

1. Press **New service** (<kbd>Ctrl</kbd>+<kbd>N</kbd>), drop a script or a folder on the window, or press
   **From Git** to clone a repository.
2. Choose the **Folder** of the project. **Detect from folder** picks the language and the file to run
   (`main.py`, `index.js`, `main.go`, a `package.json`, `go.mod`, `Cargo.toml`...).
3. Leave the mode on **Sandbox**. If the program is a server, open the **Network** tab and list its port, for
   example `8000`.
4. Press **Save**, then **Packages ▾ → Install packages** if the project has dependencies (a `requirements.txt`,
   `package.json`, `Gemfile`...), then **Start** (<kbd>F5</kbd>).

The console shows the program's output as it happens. The first lines say what the sandbox allows, for example:

```text
[19:05:20] Sandbox (Landlock + seccomp + bubblewrap): folder read & write, network on, serves 8000 for this computer
```

Close the window and the service keeps running. Open Pythonator again to see it.

## The window

* **Services**, top left: each one with its state, its isolation mode and what it runs, and CPU and memory while
  it runs. Right-click one for more.
* **Files**, under the services: the selected service's folder. Double-click a file to open it. The buttons make a
  new file or folder, fold the tree, and open the folder in your file manager.
* **Editor**, in the middle: the open files, with highlighting for every language Pythonator runs.
* **Console**, under the editor: the program's output, and a line to type into its input.
* **Plugins**, on the right: panels of the plugins that have one, such as Guard and Notify, as tabs or windows of their own.

Drag the lines between them to give any part more room.

## Closing and quitting

When you close the window while services run, Pythonator asks:

* **Keep them running**: the background service carries on. Services with **Start when Pythonator starts** checked
  start again whenever the background service starts.
* **Stop everything and quit**: every service is stopped politely (Ctrl+C or SIGTERM, then a kill after its stop
  timeout), and the background service exits.

With nothing running, closing the window quits everything.

## Where your data is kept

| System | Folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Pythonator` |
| macOS | `~/Library/Application Support/Pythonator` |
| Linux | `~/.local/share/Pythonator` |

Set `PYTHONATOR_HOME` to use another folder. Inside it:

| File or folder | What it is |
| --- | --- |
| `services.json` | Your services |
| `settings.json` | Settings, plugin settings and the permissions you granted |
| `logs/` | One rotating log per service, and `daemon.log` |
| `sandbox/` | Each sandboxed service's private home and temporary folders |
| `plugins/`, `plugin-data/` | Third-party plugins you installed, and every plugin's own data |
| `daemon.token`, `daemon.sock` | How the window proves it is you to the background service |

Everything in it is readable only by your account. No service can see this folder, in any mode but Full access.

The earlier preview builds kept services in `pythonator.json`. The first start imports them once; anything that
doesn't pass the new checks is skipped and listed.

## Keyboard shortcuts

| Keys | Does |
| --- | --- |
| <kbd>Ctrl</kbd>+<kbd>N</kbd> | New service |
| <kbd>F5</kbd> | Start the selected service |
| <kbd>Shift</kbd>+<kbd>F5</kbd> | Stop it |
| <kbd>Ctrl</kbd>+<kbd>R</kbd> | Restart it |
| <kbd>Ctrl</kbd>+<kbd>S</kbd> | Save the open file |
| <kbd>Ctrl</kbd>+<kbd>F</kbd> | Find and replace in the open file |
| <kbd>Ctrl</kbd>+<kbd>+</kbd> / <kbd>Ctrl</kbd>+<kbd>−</kbd> | Make the editor's text bigger or smaller (every open file follows) |
| <kbd>Ctrl</kbd>+<kbd>W</kbd> | Close the open file |
| <kbd>Ctrl</kbd>+click | Add another caret |

On macOS, <kbd>Ctrl</kbd> is <kbd>⌘</kbd>.

## Next

* [Services](services.md): restarts, schedules, limits and environment variables.
* [Isolation modes](isolation.md) and [Ports and APIs](networking.md): what a service may reach.
* [Plugins](plugins.md): alerts, rules and health checks.
