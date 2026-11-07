# Glossary

## AppContainer

Windows' built-in application sandbox, used by Microsoft Store apps and web browsers. A process in an AppContainer
runs under its own security identity and can open only what has been granted to that identity. Pythonator creates
one per service, named `Pythonator.<service id>`.
[Microsoft documentation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation).

## Background service

The part of Pythonator that runs the services, also called the daemon. It keeps running when the window is closed.
See [Architecture](architecture.md) and the [background service API](service-api.md).

## bubblewrap

`bwrap`, the small tool Flatpak uses to give a program its own view of the file system and its own process, IPC and
hostname namespaces, without root. Optional; Pythonator uses it on Linux when it is installed.
[Project page](https://github.com/containers/bubblewrap).

## Container image

A packaged file system with a runtime installed, such as `python:3.13-slim`. Container mode runs the service in one,
with the project folder mounted at `/app`.

## Cron

A five-field schedule format: minute, hour, day of month, month, day of week. `30 2 * * 1` means 02:30 every Monday.
[crontab(5)](https://man7.org/linux/man-pages/man5/crontab.5.html).

## Exit code

The number a program returns when it ends. 0 means success; anything else indicates failure. The restart policy
*When it crashes* restarts on any non-zero code.

## Fast-forward

A git update that only moves the local branch forward to newer commits, without creating a merge. It is not possible
if the local copy has diverged, in which case Pythonator stops rather than merging.
[git-merge](https://git-scm.com/docs/git-merge#Documentation/git-merge.txt---ff-only).

## Job object

A Windows mechanism for managing a group of processes as one unit. Pythonator puts each service in one, with *kill
on job close*, so stopping the service ends every process it started, and memory and CPU limits apply to all of them
together. [Microsoft documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## Landlock

A Linux security module (kernel 5.13 and later) that lets an unprivileged process restrict its own access to files,
TCP ports and other processes, for itself and all its descendants. The main layer of Pythonator's Linux sandbox.
[Kernel documentation](https://docs.kernel.org/userspace-api/landlock.html).

## Loopback

A computer's network interface to itself, `127.0.0.1`. A server listening only there can be reached by programs on
the same computer and nothing else.

## Manifest

A plugin's `plugin.toml`: its ID, name, version, permissions and settings. See
[Plugin API](plugin-api.md#plugintoml).

## Official plugin

A plugin in Pythonator's own `plugins/` folder whose files match the SHA-256 hashes recorded in the reviewed
catalogue.

## Port

The number a server listens on, such as `8000`. A client connects to an address and a port: `127.0.0.1:8000`.

## Process tree

A program, every program it started, and every program those started in turn. Pythonator stops, limits and measures
the whole tree.

## Sandbox

A set of restrictions under which a program runs, so that it can reach only what it needs. Pythonator's Sandbox mode
uses the operating system's own mechanisms: Landlock, seccomp and bubblewrap on Linux, Seatbelt on macOS,
AppContainer and job objects on Windows.

## Seatbelt

macOS's built-in sandbox, the mechanism behind Apple's
[App Sandbox](https://developer.apple.com/documentation/security/app-sandbox). Pythonator describes what a service
may do in a Seatbelt profile and starts it with `sandbox-exec`.

## seccomp

A Linux feature that filters the system calls a process may make. Pythonator uses it to remove network access when
the network is off, and to block calls no service needs, such as `ptrace`.
[seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html).

## SHA-256

A cryptographic hash function: any change to the input, even a single byte, produces a completely different value.
Pythonator uses it to recognise official plugins and to identify the data folder.

## Test mode

The state in which third-party plugins start: actions they request against services are recorded in their Activity
instead of being carried out.

## Token

A long random secret. The background service writes a new one to a file readable only by the user each time it
starts, and accepts only connections that present it.

## Virtual environment

A folder (usually `.venv`) containing a Python interpreter link and its own set of packages for one project, so that
projects do not share or break each other's dependencies.
[venv documentation](https://docs.python.org/3/library/venv.html).
