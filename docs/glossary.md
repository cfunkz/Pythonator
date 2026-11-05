# Glossary

## AppContainer

Windows' built-in sandbox for programs, used by Microsoft Store apps and browsers. A program in an AppContainer has
an identity of its own and can open only what is granted to that identity. Pythonator makes one per service, named
`pythonator-<id>`.

## Background service

The part of Pythonator that runs the services, also called the daemon. It keeps running when the window closes. See
[Architecture](architecture.md).

## bubblewrap

`bwrap`, the small tool Flatpak uses to give a program its own view of the file system and its own process, IPC
and host-name namespaces, without root. Optional; Pythonator uses it on Linux when it is installed.

## Container image

A packaged file system with a language installed, such as `python:3.13-slim`. Container mode runs the service in one,
with your project folder mounted at `/app`.

## Cron

A five-field way to write a schedule: minute, hour, day of the month, month, day of the week. `30 2 * * 1` is 02:30
every Monday.

## Exit code

The number a program ends with. 0 means it finished normally; anything else means it failed. Pythonator's restart
policy *When it crashes* restarts on anything but 0.

## Fast-forward

A git pull that only moves your copy ahead to the newer commits, without merging. If your copy has changes of its own,
it isn't possible, and Pythonator stops instead of merging.

## Job object

A Windows grouping of processes. Pythonator puts each service in one, with *kill on close*, so stopping the service
ends every process it started, and a memory limit applies to all of them together.

## Landlock

A Linux security feature (kernel 5.13 and later) that lets any program, without root, give up access to files,
TCP ports and other processes, for itself and everything it starts. The main layer of Pythonator's Linux sandbox.

## Loopback

The connection a computer has to itself, `127.0.0.1`. A server listening only there can be reached by programs on the
same computer and nothing else.

## Manifest

A plugin's `plugin.toml`: its id, name, version, permissions and settings. See [Plugin API](plugin-api.md#plugintoml).

## Official plugin

A plugin in Pythonator's own `plugins/` folder whose files match the SHA-256 recorded in the reviewed catalog.

## Port

The number a server listens on, such as `8000`. A program connects to an address and a port: `127.0.0.1:8000`.

## Process tree

A program and every program it started, and every program those started. Pythonator stops, limits and measures the
whole tree.

## Sandbox

A set of restrictions a program runs under, so it can reach only what it needs. Pythonator's Sandbox mode uses the
operating system's own: Landlock, seccomp and bubblewrap on Linux, Seatbelt on macOS, AppContainer on Windows.

## Seatbelt

macOS's built-in sandbox, used by Apple's own apps and services. Pythonator describes what a service may do in a
Seatbelt profile and starts it with `sandbox-exec`.

## seccomp

A Linux feature that limits which system calls a program may make. Pythonator uses it to remove the network when it is
off and to block calls no service needs, such as `ptrace`.

## SHA-256

A fingerprint of data: any change, even one byte, gives a completely different value. Pythonator uses it to recognise
official plugins.

## Test mode

How third-party plugins start: what they ask to do to your services is recorded in their Activity instead of done.

## Token

A long random secret. The background service writes a new one each time it starts, to a file only you can read, and
accepts only connections that send it.

## Virtual environment

A folder (usually `.venv`) with its own copy of Python's packages for one project, so projects don't share or break
each other's packages.
