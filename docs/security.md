# Security

This page says what Pythonator protects, against what, how, and where the limits are. To report a security problem,
use [a private advisory](https://github.com/cfunkz/Pythonator/security/advisories/new), not a public issue.

## What it protects against

| Threat | Protection |
| --- | --- |
| A service you run is malicious, or gets taken over (a bad dependency, a remote-code bug) | Sandbox or Container mode: it can't read your files and keys, write outside its folder, listen on other ports, reach your desktop session, or touch other programs |
| A service tries to read Pythonator's data (the token, other services' logs) | The data folder is never granted, in any mode but Full access, and is private to your account |
| A plugin is malicious | Plugins run in their own sandbox, receive only what their permissions allow, start in test mode when third-party, and are disabled when they flood, send garbage or crash repeatedly |
| An official plugin is swapped or edited | Official means *its files match the reviewed SHA-256 catalog*; one changed byte makes it third-party, with the warning, test mode and no permissions |
| Another user on the computer, or a program, talks to the background service | The socket is your account's only (0600 / a per-user pipe) and needs a random token from a file only you can read; the token is compared in constant time |
| A crafted message to the background service | Every request is a strict typed model with limits; unknown fields are refused; one line is at most 1 MB; a slow reader is cut off, never waited for |
| A cloned repository runs code on clone or pull | Hooks off, submodules off, fsmonitor off, only `https` and `ssh` transports, addresses checked, fast-forward only |
| Arguments that look different from what runs | Programs start from argument lists, never a shell; NUL, bidi and zero-width characters are refused |
| `LD_PRELOAD` and friends | Refused as service variables |
| Secrets in output | Values of secret-looking variables are masked in the console, logs and plugin messages |
| Pythonator itself crashes | Services keep running; the next start finds them by process id and start time |

## How each system enforces the sandbox

### Linux

* **Landlock** (kernel 5.13+; network rules 6.7+, scoping 6.12+): the service may read the system folders, its
  language's installation and its project, write its project and its private home and temp folders, and nothing
  else. With network on, it may bind and connect only on the listed TCP ports; signals and abstract Unix sockets
  outside its own sandbox are refused.
* **seccomp** (through `libseccomp`): with the network off, no sockets but Unix ones. Always: no `ptrace`, `bpf`,
  `io_uring`, kernel module loading, `kexec`, `perf_event_open`, `userfaultfd`, mounts, joining other namespaces,
  or typing into your terminal (`TIOCSTI`).
* **bubblewrap**, when installed and allowed to make user namespaces: a private `/tmp`, PID, IPC and UTS
  namespaces, only the needed folders mounted, every capability dropped, and the service dies with its parent.

Each layer works alone. **System check** shows which are active. Ubuntu 24.04 and later restrict unprivileged user
namespaces with AppArmor; bubblewrap from the distribution's package has the profile it needs.

### macOS

A Seatbelt profile, passed to `sandbox-exec`, that denies by default and allows the same folders, with every path
passed as a parameter rather than pasted into the profile. Network: outbound connections only when on, listening only
on the listed ports, on `127.0.0.1` unless *Other computers* is chosen. Apple marks `sandbox-exec` as deprecated but
uses the same mechanism for its own services; if it is ever removed, the sandbox reports *not enforced* and services
don't start in Sandbox mode.

### Windows

* An **AppContainer** per service: a separate identity with no access to your files unless granted. Pythonator
  grants the project and runtime folders, records each grant, and removes them when the service is removed.
* A **job object** per service, with *kill on close*: the program starts suspended, is put in the job, and only
  then runs, so every process it ever starts is inside.
* Network capabilities only when the network is on. Reaching its ports needs
  [Make reachable](networking.md#windows-make-reachable), which adds a loopback permit and firewall rules for this AppContainer only,
  for `127.0.0.1` and, if asked, private networks.
* Python services get a `sitecustomize` that makes `os.mkdir(..., 0o700)` produce a folder only the service can open
  (the fix for CVE-2024-4030, for Pythons older than 3.12.4).

### Container

Docker or Podman with every capability dropped, `no-new-privileges`, a read-only image, a process limit, only the
project mounted, and `--network none` when off. A container is as strong as the engine: rootless Podman is
stronger than a Docker daemon running as root.

## What it doesn't do

* **Full access** is no sandbox. Pythonator asks once per service and shows it in red.
* **A sandbox is not a virtual machine.** A kernel bug can break out of any sandbox on the same kernel; Container
  mode on a VM-based engine (Docker Desktop, Podman machine) adds that layer.
* **Linux can't restrict the address a service listens on**, only the port. A service told `HOST=127.0.0.1` can
  choose `0.0.0.0`. Use your firewall, or Container mode.
* **The network is all or ports-only.** A service with network on can connect to any address on its allowed ports
  (on macOS, any address). Use the [Guard](plugins.md#guard) plugin to watch connections.
* **Pythonator doesn't scan code.** It contains what runs; it doesn't decide whether code is good.
* **It runs as you.** It doesn't need, and shouldn't have, administrator rights; System check warns when it has them.

## Supply chain

* Six direct dependencies, all widely used. `requirements.lock` pins every package, direct and indirect, with
  SHA-256 hashes.
* Every change, and a weekly run, checks the lock files with `pip-audit`. CodeQL's default setup is turned on in the
  repository settings. Dependabot proposes updates after a 7-day cool-down, so a just-published malicious version
  isn't picked up the same day.
* PyQt6 is held below 6.12.0, whose type stubs are broken; Qt's JSON parsers aren't used at all.
* GitHub Actions are pinned to commit hashes, and workflows have read-only permissions by default.

## Reporting

Please report problems privately through
[GitHub's security advisories](https://github.com/cfunkz/Pythonator/security/advisories/new). See
[SECURITY.md](../SECURITY.md) for what to include and what to expect.
