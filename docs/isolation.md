# Isolation

Every service runs inside layers, outermost first. The security stack in the window draws them; the plan lists them;
the first console line of every run repeats them. Each layer has a status:

* **enforced**: the kernel or the engine enforces it on this machine.
* **reduced**: enforced, but weaker here than it could be, and the plan says why and how to fix it.
* **not enforced**: only in Full access, labelled as such.
* **off**: not needed (for example *Ports* when no port is open).

If a layer a mode needs can't be built on this machine, the start is **refused** with a code and a fix. Pythonator
never runs a service with a layer quietly missing.

## What each mode enforces, per OS

Generated from the compiler and the reference machines (`python -m pythonator.service.check --doc`); a test
fails when this table and the code disagree.

<!-- matrix:start -->
#### Sandbox

| Protection | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Process tree (Stop stops all) | enforced · cgroup scope | reduced · process group + run marker | enforced · job object |
| Memory · CPU · tasks | enforced · cgroup v2 | reduced · watchdog | enforced · job object |
| Files: folder only | enforced · bubblewrap + Landlock | enforced · Seatbelt profile | enforced · AppContainer + ACL grants |
| Syscall filter | enforced · seccomp · no_new_privs · no capabilities | reduced · Seatbelt operations | reduced · AppContainer token |
| Traffic: allowed hosts | enforced · own namespace + egress gate | enforced · Seatbelt profile | refused (NET-WIN-001) |
| Block 127.0.0.1 programs | enforced · own network namespace | enforced · Seatbelt profile | enforced · AppContainer |
| Ports: this computer / LAN | enforced · gate listener | enforced · Seatbelt bind rules | refused (NET-WIN-002) |

#### Container

| Protection | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Process tree (Stop stops all) | enforced · container | enforced · container | enforced · container |
| Memory · CPU · tasks | enforced · podman cgroup | enforced · podman cgroup | enforced · podman cgroup |
| Files: folder only | enforced · read-only image + this folder | enforced · read-only image + this folder | enforced · read-only image + this folder |
| Syscall filter | enforced · podman default seccomp · no capabilities | enforced · podman default seccomp · no capabilities | enforced · podman default seccomp · no capabilities |
| Traffic: allowed hosts | refused (engine network) | refused (engine network) | refused (engine network) |
| Block 127.0.0.1 programs | enforced · pasta --no-map-gw | enforced · pasta --no-map-gw | enforced · pasta --no-map-gw |
| Ports: this computer / LAN | enforced · published ports | enforced · published ports | enforced · published ports |

#### WSL

| Protection | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Process tree (Stop stops all) | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · own Linux user id |
| Memory · CPU · tasks | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · cgroup v2 in WSL |
| Files: folder only | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · bubblewrap in Pythonator's WSL |
| Syscall filter | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · seccomp · no_new_privs · no capabilities |
| Traffic: allowed hosts | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · own namespace + egress gate in WSL |
| Block 127.0.0.1 programs | refused (RUN-WSL-001) | refused (RUN-WSL-001) | enforced · WSL virtual machine |
| Ports: this computer / LAN | refused (RUN-WSL-001) | refused (RUN-WSL-001) | reduced · WSL localhost forwarding |

#### Full access

| Protection | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Process tree (Stop stops all) | enforced · cgroup scope | reduced · process group + run marker | enforced · job object |
| Memory · CPU · tasks | enforced · cgroup v2 | reduced · watchdog | enforced · job object |
| Files: folder only | not-enforced · runs as you | not-enforced · runs as you | not-enforced · runs as you |
| Syscall filter | not-enforced · runs as you | not-enforced · runs as you | not-enforced · runs as you |
| Traffic: allowed hosts | refused (NET-FULL-001) | refused (NET-FULL-001) | refused (NET-FULL-001) |
| Block 127.0.0.1 programs | not-enforced · runs as you | not-enforced · runs as you | not-enforced · runs as you |
| Ports: this computer / LAN | not-enforced · runs as you | not-enforced · runs as you | not-enforced · runs as you |
<!-- matrix:end -->

The table is the reference machine for each OS with everything installed. **This** machine's column is in System
check, and can be weaker (for example *reduced* limits on Linux without a systemd user session).

## Sandbox mode on Linux

| Layer | How | What it means |
| --- | --- | --- |
| Process tree | bubblewrap PID namespace, `--die-with-parent`, inside a systemd scope | Stop ends every child; nothing escapes as an orphan |
| Limits | systemd scope (cgroup v2: MemoryMax, MemorySwapMax=0, CPUQuota, TasksMax); else a watchdog (*reduced*) | memory, CPU and task caps |
| Files | bubblewrap mount namespace **and** Landlock | its folder read-write, the runtime read-only, nothing else of your home |
| Syscalls | libseccomp filter, `no_new_privs`, new session, no capabilities | no ptrace, mounts, keyrings, bpf, io_uring, user namespaces, terminal injection or setuid files |
| Network | its own network namespace | no network at all, unless Traffic is on |
| Programs on 127.0.0.1 | its own namespace; with traffic, `pasta --no-map-gw` or the gate's relay | can't reach databases or tools listening on your localhost |
| Ports | `pasta` port forwarding, or the gate's listener | only the ports you opened, to this computer or your LAN |

bubblewrap is required: without it Sandbox mode and plugins are refused (`RUN-LNX-001`, `PLG-LNX-001`), never run in
weaker isolation. On Ubuntu 24.04 and later, AppArmor blocks the user namespaces bubblewrap needs until you allow them;
System check shows the exact command.

### Files, twice

Bubblewrap builds the service's view: `/usr` and the few files under `/etc` a runtime needs (certificates, time
zone) read-only, a private `/tmp`, an empty `/home` with a home of its own (`/home/service`), and its folder at
`/app`, which is also its working folder. Extra folders appear under `/data/`, read-only unless you said
otherwise. Inside that view, a small launcher (`pythonator/jail/launcher.py`, standard library only, started with
`python3 -I -S`) applies **Landlock** and the **seccomp** filter before the program starts: even if a mount were
wrong, the kernel still refuses any path outside the list. If either can't be applied, the launcher exits with code
126 and the service doesn't run.

### The seccomp filter

Built with libseccomp (`libseccomp.so.2`), which knows every architecture's numbers and refuses foreign ones:

* **EPERM**: ptrace, process_vm_readv/writev, keyctl, add_key, request_key, bpf, perf_event_open, userfaultfd, mount
  and the new mount API, pivot_root, chroot, setns, kexec, module loading, open_by_handle_at, swapon, reboot, acct,
  syslog, iopl and ioperm; `clone` and `unshare` with a new user namespace; the TIOCSTI and TIOCLINUX ioctls;
  `chmod` family calls that add setuid, setgid or write for others; with Traffic off, every socket family but Unix.
* **ENOSYS** (programs fall back): io_uring, clone3, openat2. Calls the installed libseccomp doesn't know are
  filtered by number.

## Sandbox mode on macOS

A Seatbelt profile run by `sandbox-exec`: `(deny default)`, then the system, the runtime and the service's folders,
written broad to narrow so a read-only folder inside a writable one stays read-only. Every path is a `-D` parameter,
never text pasted into the profile. `/dev` is literals only (null, zero, tty, ptmx, dtracehelper) plus `/dev/fd/`, so
your Terminal tabs (`/dev/ttys*`) aren't reachable. If Apple's base profiles are needed on this macOS version, the
profile builds on `bsd.sb`, then `system.sb`. Limits are a watchdog (*reduced*); Stop ends the process group and
anything carrying the run's marker. If `sandbox-exec` doesn't work, Sandbox mode is refused (`RUN-MAC-001`).

## Sandbox mode on Windows

Each service is an **AppContainer** (`Pythonator.<name>`) inside a **job object**. The process starts suspended,
joins the job, then resumes; one that can't join is never resumed. The job sets memory and CPU caps, kills the whole
tree when it closes, and adds UI restrictions (clipboard, desktop, global atoms, display settings). Only the
standard handles are inherited. The AppContainer can read only what is granted: its folder (read-write), extra
folders, and the runtime's install folder (read-only, never a PATH-only folder or WindowsApps). Each grant is
recorded in the ledger before it is made, under a named mutex, and taken back when the service changes, is removed,
or Pythonator is uninstalled. A read-only grant adds a deny entry when anything inherited could still write.

* Programs that would run outside the AppContainer are refused: WSL's `bash.exe` and `wsl.exe` (`RUN-WIN-003`), and
  `.bat`/`.cmd` files, which run through `cmd.exe` (`RUN-WIN-004`).
* Every Python in the sandbox loads a small `sitecustomize` so `mkdir(0o700)` and temporary files work (CVE-2024-4030).
* There is no Make reachable: ports are refused (`NET-WIN-002`) and allowed hosts too (`NET-WIN-001`). Use WSL mode
  for both.

## WSL mode (Windows)

Pythonator installs its **own** Debian distribution (Engines → WSL → Set up): no
Windows drives mounted, no Windows interop, bubblewrap and libseccomp from Debian's signed archive. Each run is the
Linux Sandbox environment inside it, started by `wsl.exe` from System32 with an argument list; limits are a cgroup inside
the distribution (the run refuses to start without one). Variables cross only through `WSLENV`. Stop ends the run
inside Linux, also after a crash. Your own WSL distributions are never used.

## Container mode

The container engine provides the process tree, files (read-only image plus your folder at `/app`), its default
seccomp profile with no capabilities, and the network. Pythonator always adds `--read-only`, tmpfs `/tmp` and home,
`--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`, `--memory` with equal `--memory-swap`,
`--cpus`, its labels, and your user id (`--user uid:gid` on Docker, `--userns keep-id` on Podman). Each service with
traffic gets its own network (`pythonator-<name>`), removed with it; Traffic off is `--network none`. The image must
be an OCI reference (digest pins welcome); option-like values are refused. Folder paths with `,`, `:` or quotes are
refused (`RUN-CTR-002`). Values of variables and secrets go through the engine's environment, never the command
line. Rootless Podman is stronger than rootful Docker; engines that run in a VM add a kernel boundary.

## Full access

Runs as you. Files and Network are **not enforced**, and the plan says so. The process tree and limits still apply
(a systemd scope on Linux, a job object on Windows), so Stop still ends every child. An allow-list is refused
(`NET-FULL-001`) because nothing could enforce it. A Full access service can't share a folder with an isolated
service (`RUN-FOLDER-001`), so an isolated service can never plant code that runs as you.

## Folders

Some folders can never be given to a service, in any mode, after links are resolved:

| Code | Refused |
| --- | --- |
| POL-PATH-001 | a relative path |
| POL-PATH-002 | your whole home folder |
| POL-PATH-003 | a whole drive |
| POL-PATH-004 | system folders (`/etc`, `/usr`, `/var`, `/opt`, `/srv`, `/nix`, `/snap`, `/run`, `/Applications`, `C:\Windows`, Program Files, ProgramData…) and shared folders themselves (`/tmp`, `/mnt`, `/media`; a folder inside is fine) |
| POL-PATH-005 | a folder that contains your home |
| POL-PATH-006 | secret folders: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.npmrc`, `~/.pypirc`, `~/.git-credentials`, `~/.cargo/credentials`, container engine folders (Podman, Colima, Lima, Rancher, OrbStack, Docker Desktop), browser profiles, keyrings, `AppData`, `Library/Application Support`… |
| POL-PATH-007 | a folder that contains one of those |
| POL-PATH-008 | writing to places that run code when you log in: shell start files, autostart and application entries, systemd user units, PowerShell profile folders (Documents, also under OneDrive), `~/.cargo/bin`, `~/go/bin`, user site-packages |
| POL-PATH-009 | Pythonator's own data folder |
| POL-PATH-010 | `\\wsl$` and `\\wsl.localhost` paths (your drives under another name) |
| POL-PATH-011 | Pythonator's own program folder and Python prefixes |
| POL-PATH-012 | writing to a folder on your `PATH` inside your home |

## Plugins

Plugins run in stricter isolation than services: no network, no home, and their own code read-only. See
[Plugins](plugins.md#plugin-isolation).

## Known limitations

These are true in 0.0.1. Each one is also an open item in [Security cases](security-cases.md).

* **macOS**: limits are a watchdog (it can overshoot before it acts, and there is no process cap), and a child that
  starts a new session and clears its environment can outlive Stop.
* **Windows**: the data folder's permissions aren't checked at start (it lives in your own `LocalAppData`); Node.js
  under the AppContainer isn't verified on a real machine yet; LPAC isn't used; a GUI program gets no WM_CLOSE
  before the job ends it.
* **Windows**: when `pythonatord` ends, the job objects end every service with it.
* **Linux**: a sandboxed service can make files in its own folder readable by other accounts (seccomp refuses
  adding write for others, not read).
* **Container**: Podman's network `isolate` option and its version aren't checked.
* **Releases** aren't code-signed or notarized yet.
* **Full access** provides no isolation.
