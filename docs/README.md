# Pythonator documentation

Pythonator is a service manager for your own computer. It runs your web apps, APIs, workers, databases and
scheduled jobs on Windows, macOS and Linux, each one in isolation, kept running by a background service, and changed
only with your approval.

| Read this | When you want to |
| --- | --- |
| [Getting started](getting-started.md) | install it and run your first service |
| [Services](services.md) | understand modes, runtimes, restarts, health checks, schedules, limits, logs and secrets |
| [Isolation](isolation.md) | know what each layer does on each OS, what *reduced* means, and the known limitations |
| [Networking](networking.md) | let a service reach some hosts or the internet, or open a port |
| [Plugins](plugins.md) | use Guard, Notify, the AI assistant or Builder, grant permissions, write your own |
| [Command line](cli.md) | `pythonatorctl version` and `pythonatorctl uninstall` |
| [System check](system-check.md) | see what protects services on this machine and how fast it is |
| [Security model](security.md) | know what is defended, how, and what isn't |
| [Security cases](security-cases.md) | see how each of the 286 cases from earlier audits is answered |
| [Earlier fixes](earlier-fixes.md) | find where each fix of the old app (0.0.1 to 0.0.19) lives now |
| [Architecture](architecture.md) | find your way around the code |
| [Development](development.md) | run the tests, change the code, make a release |
| [Design](design/README.md) | see the approved design the window is built from |

## Words used everywhere

* **Service**: one program Pythonator runs for you: a folder, a command and how it should run. It can be a web app,
  an API, a worker, a database or a job that runs on a schedule.
* **Mode**: *Sandbox* (kernel-enforced isolation, the default), *Container* (Podman or Docker), *WSL* (Windows: the Linux sandbox
  in Pythonator's own distribution) or *Full access* (runs as you).
* **Plan**: what the service will be allowed to do on this machine, layer by layer, worked out before it runs.
* **Layer**: one protection, such as *Files* or *Network*. Each one is *enforced*, *reduced* (weaker on this machine,
  and it says why) or *not enforced* (Full access only). Pythonator never runs a layer "on trust".
* **Background service**: `pythonatord`, the process that owns your services. The window only
  ask it to do things.
