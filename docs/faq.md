# FAQ

## Using it

### Does closing the window stop my services?

No, unless you choose **Stop everything and quit**. Services keep running in the background service; open Pythonator
again to see them. With nothing running, closing the window quits everything.

### How do I start Pythonator when I log in?

Add `pythonator` to your system's startup programs (Windows: the Startup folder or Task Scheduler; macOS: Login
Items; Linux: your desktop's autostart, or a systemd user service running `pythonator --daemon`). Tick **Start when
Pythonator starts** on the services that should come up with it.

### My service says "failed: Gave up after 5 restarts"

It crashed five times within ten minutes, so Pythonator stopped trying. The console shows why it crashed. Fix it and
press Start. To change the number, see **Give up after** in [Restarts](services.md#restarts).

### The console shows "The window can't keep up; showing the newest output only"

The program prints faster than the window can draw. Nothing is lost: the log file has every line. The window catches
up when the program slows down.

### Where are the logs?

In `logs/` in your [data folder](getting-started.md#where-your-data-is-kept), one file per service, rotated at 10 MB.

### Can I move my services to another computer?

Copy `services.json` and `settings.json` from the data folder. Folders in it are absolute paths, so the projects
must be at the same paths, or edit them.

## Sandbox

### My program can't read a file outside its folder

That's the sandbox working. Add the folder under **More folders** on the Isolation tab, read only if it only needs to
read it. Your home folder, Pythonator's data folder and folders of keys can't be added.

### "The sandbox couldn't start it. Nothing ran unsandboxed."

The operating system refused part of the sandbox. The line above it in the console says which. Common causes:

* Linux without Landlock (kernels before 5.13, or Landlock turned off): update the kernel, or use Container mode.
* Linux where unprivileged user namespaces are blocked: bubblewrap is skipped automatically; if the line names
  bubblewrap, install your distribution's `bubblewrap` package, which comes with the permission it needs.
* Windows editions or policies that forbid AppContainers.

**System check** shows what this computer enforces.

### My server runs, but I can't open it in the browser

* Check its port is listed on the **Network** tab, and the program listens on that port (the header shows the ports
  it actually listens on).
* On **Windows**, press **Make reachable…** once: Windows keeps sandboxed servers unreachable, even from the same
  computer, until an administrator allows it. See [Ports and APIs](networking.md#windows-make-reachable).
* On **Linux 6.7+** and **macOS**, a sandboxed program may only listen on the listed ports. A framework that picks
  its own port (`3000` instead of `$PORT`) is refused: list that port, or make it use `PORT`.

### Other computers can't reach my server

Choose **Other computers on home or work networks too** under *Who may connect*, make the program listen on
`0.0.0.0` (or leave `HOST` unset), and check the computer's firewall. On Windows, press Make reachable again after
changing it.

### Can a sandboxed service use my GPU?

Not in this version: the sandbox and the container give a service only the basic devices, not the graphics card's.
Use Full access for code you trust that needs the GPU.

### Is Container mode safer than Sandbox?

Both keep the service to its folder. Containers add their own file system and a separate network namespace, and on
Docker Desktop and Podman machine a whole virtual machine. Sandbox needs nothing installed and starts faster. Choose
Container when you'd rather not have the service see your system folders at all, or need the program's own Linux
environment.

## Languages

### Python uses the wrong packages

Each service has its own environment by default (**Own environment** on the General tab). For Python, Start creates
`.venv` in the project (and installs `requirements.txt` or `pyproject.toml`) when it's missing or broken, so a service
never runs on your global packages. Node.js and Bun get `node_modules`, Ruby `vendor/bundle`, PHP `vendor`, and Deno,
Go, .NET and Rust a package cache of their own. **Packages ▾ → Recreate environment** deletes it and installs again
from scratch. Turn **Own environment** off to run on the runtime as it is installed.

### My language isn't in the list

Use **Custom command**. To add it to the list, see [Adding a language](adding-a-language.md).

### npm or pip can't download anything during Install packages

Install packages turns the network on for the install, even when the service has it off. Proxy variables
(`HTTPS_PROXY`, `NO_PROXY`) from where Pythonator was started are passed on; you can also set them on the service's
Environment tab. The console shows the installer's own error.

## Plugins

### What's the difference between official and third-party plugins?

Official plugins are part of Pythonator and checked by hash. Everything else is third-party: it starts in test mode
with no permissions. Both run in a sandbox. See [Plugins](plugins.md).

### My plugin was disabled

Its Activity says why: it crashed three times in ten minutes, sent too many messages, or sent something that isn't a
protocol message. Fix it and tick **Enabled** again.

### Can I write a plugin in another language?

Plugins are Python, using the [SDK](plugin-api.md). They can start no other programs in their sandbox, but a plugin can
talk to anything over the network with the `network` permission. To drive Pythonator from another language, use the
[background service API](service-api.md) instead.

## Upgrading

### Does it keep services from the earlier preview builds?

Yes. The preview builds published before 0.0.1 kept services in `pythonator.json`, and the first start imports that
file once. Services that don't pass the new checks are skipped and listed.
