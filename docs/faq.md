# FAQ

## General

### Does closing the window stop the services?

No, unless **Stop everything and quit** is chosen. Services run in the background service, independently of the
window; reopening Pythonator shows them again. If nothing is running, closing the window quits Pythonator.

### How can Pythonator start at sign-in?

Tick **Start Pythonator when I sign in** (**Start Pythonator with Windows** on Windows) under **Settings**. This adds
a per-user entry only (a `Run` registry value on Windows, a LaunchAgent on macOS, an
[XDG autostart](https://specifications.freedesktop.org/autostart-spec/latest/) entry on Linux), so no administrator
rights are needed. Pythonator then opens in the tray. Services with **Start when Pythonator starts** ticked start in
list order, as configured under **Settings → Start services**.

On a headless Linux server, run `pythonator --daemon` from a systemd user service instead.

### A service shows "failed: Gave up after 5 restarts"

It crashed five times within ten minutes, so Pythonator stopped restarting it. The console shows the cause. After
fixing it, choose **Start**. The limit is **Give up after** on the **Schedule & restarts** tab; see
[Restarts](services.md#restarts).

### The console shows "The window can't keep up; showing the newest output only"

The program writes faster than the window can draw. No output is lost: the log file contains every line, and the
console catches up once the program slows down.

### Where are the logs?

In `logs/` in the [data folder](getting-started.md#the-data-folder), one file per service, rotated at 10 MB by default.
**Open log** in a service's context menu opens the current file.

### Can services be moved to another computer?

Copy `services.json` and `settings.json` from the data folder. Project folders are stored as absolute paths, so the
projects must be at the same paths on the new computer, or the paths edited.

## Sandbox

### A program cannot read a file outside its folder

This is the sandbox working as intended. Add the folder under **More folders** on the **Isolation** tab, read-only
if writing is not needed. The home folder, Pythonator's data folder and folders holding keys cannot be added.

### "The sandbox couldn't start it. Nothing ran unsandboxed."

The operating system refused part of the sandbox; the preceding console line says which part. Common causes:

* Linux without Landlock (kernels before 5.13, or Landlock disabled): update the kernel, or use Container mode.
* Linux with unprivileged user namespaces blocked: bubblewrap is skipped automatically. If the message names
  bubblewrap, install the distribution's `bubblewrap` package, which ships the AppArmor profile it needs
  ([background](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces)).
* Windows editions or group policies that forbid AppContainers.

**System check** shows what the computer enforces.

### A server runs, but the browser cannot open it

* Check that its port is listed on the **Network** tab and that the program listens on it. The header shows the ports
  it actually listens on.
* Read the console. A start reports a port that is already in use (and by which program) or, on Windows, reserved by
  the system: Hyper-V, WSL and Docker reserve port ranges that can change at each restart, and
  `netsh interface ipv4 show excludedportrange protocol=tcp` lists them. Choose another port in that case. A service
  that is still not listening 30 seconds after starting gets a note describing what its processes are doing.
* **Windows**: sandboxed servers are unreachable, even from the same computer, until an administrator allows it.
  Choose **Open** and accept **Make reachable** (or **Configure → Network → Make reachable…**). See
  [Make reachable](networking.md#windows-make-reachable).
* **Windows**, Node.js apps (Vite, React, Vue) that used a whole core and printed nothing: fixed in 0.0.15. See
  [Node.js programs that start other programs](isolation.md#windows-nodejs-programs-that-start-other-programs).
* **Linux 6.7+ and macOS**: a sandboxed program may listen only on the listed ports. A framework that picks its own
  port (`3000` rather than `$PORT`) is refused; list that port or configure the program to use `PORT`. On Linux it
  may also bind a port chosen by the system (port 0), which tools such as Next.js use for their workers.

### Other computers cannot reach a server

Choose **Other computers on home or work networks too** under **Who may connect**, make the program listen on
`0.0.0.0` (or leave `HOST` unset), and check the computer's firewall. On Windows, run **Make reachable** again after
changing this setting.

### Can a sandboxed service use the GPU?

Not in this version. The sandbox and containers expose only basic devices, not graphics devices. Use Full access for
trusted code that needs the GPU.

### Is Container mode safer than Sandbox mode?

Both confine the service to its folder. A container also has its own file system and network namespace, and on
Docker Desktop or Podman machine runs inside a virtual machine. Sandbox mode needs nothing installed and starts
faster. Container mode suits a service that should not see the host's system folders at all, or that needs a
specific Linux userland.

## Languages

### Python uses the wrong packages

By default each service has its own environment (**Own environment** on the **General** tab). For Python, a start
creates `.venv` in the project when it is missing or broken, and installs `requirements.txt` or `pyproject.toml`
into it, so a service never runs against globally installed packages. Node.js and Bun use `node_modules`, Ruby
`vendor/bundle`, PHP `vendor`, and Deno, Go, .NET and Rust get a private package cache. **Packages ▾ → Recreate
environment…** deletes the environment and installs again. Untick **Own environment** to use the runtime as installed.

### "This project needs Node.js 22.22.3 or newer"

Before a start, Pythonator compares the installed runtime with the versions the project declares: `engines` in
`package.json`, `requires-python` in `pyproject.toml`, the `go` line in `go.mod`, `rust-version` in `Cargo.toml` and
`php` in `composer.json`. The Project builder does the same for each template. Install the version named in the
note, then start again. The note does not block the start, because a declared range can be stricter than necessary.

### A language is missing from the list

Use **Custom command**. To add it to the list, see [Adding a language](adding-a-language.md).

### npm or pip cannot download anything during Install packages

Install packages enables the network for the install, even when the service has it disabled. Proxy variables
(`HTTPS_PROXY`, `NO_PROXY`) from Pythonator's own environment are passed on, and can also be set on the service's
**Environment** tab. The console shows the installer's own error.

## Plugins

### What distinguishes official and third-party plugins?

Official plugins ship with Pythonator and are verified by SHA-256 hash. Any other plugin is third-party: it starts in
test mode with no permissions. Both kinds run in a sandbox. See [Plugins](plugins.md).

### A plugin was disabled

Its **Activity** log states the reason: three crashes in ten minutes, too many messages, or a message that is not
part of the protocol. Fix the cause and tick **Enabled** again.

### Can plugins be written in other languages?

No. Plugins are Python, using the [SDK](plugin-api.md), and cannot start other programs in their sandbox. With the
`network` permission a plugin can talk to any network service. To control Pythonator from another language, use the
[background service API](service-api.md).

## Upgrading

### Are services from the preview builds kept?

Yes. The preview builds published before 0.0.1 stored services in `pythonator.json`; the first start imports that
file once. Services that fail the current validation are skipped and listed.
