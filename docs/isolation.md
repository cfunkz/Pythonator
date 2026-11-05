# Isolation modes

Every service runs in one of three modes, chosen on its **Isolation** tab. New services use **Sandbox**
(`default_mode` in `settings.json` changes that).

| Mode | What the program can reach | What it needs |
| --- | --- | --- |
| **Sandbox** | Its project folder, its language's own files, and the network only if you turn it on, on the ports you list | Nothing: built into Windows, macOS and Linux |
| **Container** | Only the project folder, mounted at `/app` in a container image | Docker or Podman |
| **Full access** | Everything your account can read, change and connect to | Your trust in the code |

One rule holds in every mode: **if a protection can't be applied, the service doesn't start.** It never runs with
less than its mode promises. The console's first line says exactly what was applied.

## Sandbox

The sandbox is built from what the operating system already has. No administrator rights, no extra software.

A sandboxed service **can**:

* read its project folder, and change files in it (untick **May change files in its folder** for read only);
* read its language's installation (the Python, Node or Java it runs with, and a project's `.venv`);
* use a private home and temporary folder of its own, which Pythonator keeps in the data folder;
* use the network, when **Network** is on, and listen only on the ports you list;
* use the **More folders** you add, read only or read and write.

It **can't**:

* read your home folder, documents, other projects, SSH and cloud keys, browser profiles, or Pythonator's own data
  (the token, the logs, other services' sandboxes);
* write anywhere else, including system folders;
* listen on a port you didn't list, or use the network at all when it is off;
* see or signal your other programs, or attach a debugger to them;
* reach your desktop session: the display, the clipboard, D-Bus, the SSH agent and the keyring are not passed in.

**More folders** can't be your whole home folder, a whole drive, Pythonator's data folder, or a folder of keys and
passwords (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`, `.netrc`...). Pythonator refuses them with the reason.

### How each system enforces it

| System | Files | Network | Processes |
| --- | --- | --- | --- |
| Linux | [Landlock](glossary.md#landlock) rules on the folders above; [bubblewrap](glossary.md#bubblewrap), when installed, also gives the service its own view of the file system | Landlock TCP rules allow exactly the listed ports (Linux 6.7 and later); [seccomp](glossary.md#seccomp) removes sockets entirely when the network is off | New PID, IPC and user namespaces with bubblewrap; Landlock scoping and seccomp block `ptrace`, `bpf`, `io_uring` and other kernel attack surface |
| macOS | A [Seatbelt](glossary.md#seatbelt) profile that denies everything, then allows the folders above | Outbound connections and listening only on the listed ports, on 127.0.0.1 unless you chose other computers | Can't signal or inspect processes outside its own |
| Windows | An [AppContainer](glossary.md#appcontainer) per service; Pythonator grants it its folders and records each grant, so removing the service takes them back | Network capabilities only when Network is on; reaching its ports needs [Make reachable](networking.md#windows-make-reachable) | A [job object](glossary.md#job-object) holds the whole process tree; nothing escapes a stop |

**System check** in the toolbar lists each protection and whether this computer enforces it. On Linux,
*Only this computer reaches its ports* shows **not enforced**: Landlock can say which port a program may use but
not which address, so a program told `HOST=127.0.0.1` (Pythonator sets it) can still choose `0.0.0.0`. Use your
firewall, or Container mode, when that matters.

### When the sandbox can't start

If the sandbox can't be set up (an old kernel without Landlock, a locked-down macOS, a Windows edition without
AppContainers), the service shows **failed** with the reason and nothing runs. Pythonator never falls back to
running it without the sandbox. Use Container mode, or Full access if you trust the code.

## Container

Container mode runs the service with **Docker** or **Podman** (Podman first on Linux, as it needs no root daemon).
The project folder is mounted at `/app` and is the working folder.

Every container starts with:

| Flag | Why |
| --- | --- |
| `--cap-drop ALL` | No Linux capabilities: it can't change the network, mount, or load modules |
| `--security-opt no-new-privileges` | `setuid` programs inside can't gain rights |
| `--read-only` with a small `/tmp` and home | The image can't be changed; only the project folder can |
| `--pids-limit 1024` | A fork bomb stops at a thousand processes |
| `--network none` | When Network is off |
| `-p 127.0.0.1:PORT:PORT` | Its ports for this computer only, or `-p PORT:PORT` for other computers too |
| `--user` your id (Docker) or `--userns keep-id` (Podman) | Files it writes in the project are yours |
| `--memory` | When a memory limit is set |

Pythonator never adds `--privileged`, `--cap-add`, `--device`, `--network host` or another mount. Variables reach the
container through the engine's environment (`-e NAME`), so their values don't show in a process list.

The **image** defaults to the language's official slim image (`python:3.13-slim`, `node:22-slim`, `golang:1.24`...)
and can be any image you name, pinned by digest if you like (`python:3.13-slim@sha256:...`). For Python, Install
packages makes `/app/.venv-container`, which the container then runs with, so it doesn't clash with a `.venv` made
for your computer.

To let a container reach another one, such as a database, see
[Connecting to other containers](networking.md#connecting-to-other-containers).

## Full access

Full access runs the program as you, like a terminal would. It can read your files and keys, install things, and
connect anywhere. Pythonator asks before the first start of each Full access service and shows the mode in red.
Even here, the program runs in its own process group (a job object on Windows), so stopping it ends everything it
started.

Use it for tools that must reach your desktop or your whole disk, and only for code you trust.

## Which mode to choose

* **A bot or an API of your own:** Sandbox, Network on, its port listed.
* **Code you just cloned and haven't read:** Sandbox with Network off first, or Container.
* **Something that needs a database or other services in containers:** Container, on their network.
* **A tool that must control your desktop or reach every folder:** Full access, if you trust it.

The [Guard](plugins.md#guard) plugin can enforce choices like these for every service, for example *no Full access*
and *only ports 8000-8100*.
