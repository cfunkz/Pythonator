# System check

**System check** (the shield on the left) answers three questions about *this* machine.
Nothing on it is static text: every number is measured here.

## Probes: what can protect a service here

Each OS shows its own probes:

| OS | Probe | ✓ when | If not |
| --- | --- | --- | --- |
| Linux | bubblewrap | `bwrap` is installed and builds a jail | Sandbox mode and plugins are refused: install `bubblewrap` |
| Linux | bubblewrap version | at or above the CVE floor | a warning with the version to update to |
| Linux | AppArmor user namespaces | not restricted | bubblewrap may be refused; the fix is the sysctl or the AppArmor profile |
| Linux | libseccomp | `libseccomp.so.2` loads | Sandbox mode is refused: install `libseccomp2` |
| Linux | Landlock | the kernel offers it (ABI shown) | Sandbox mode is refused: Linux 5.13 or newer |
| Linux | Limits (cgroup v2) | a systemd user session exists | limits fall back to the watchdog (*reduced*): `loginctl enable-linger $USER` |
| Linux | pasta | `pasta` is installed | services with traffic are refused: install `passt` |
| macOS | Seatbelt | `sandbox-exec` runs a test profile | Sandbox mode is refused (`RUN-MAC-001`) |
| macOS | Limits | always a warning: memory by watchdog, no CPU or task caps | use Container mode for hard limits |
| Windows | AppContainer | a test AppContainer starts | Sandbox mode is refused |
| Windows | Job objects | always | |
| Windows | WSL mode | Pythonator's distribution is set up | Engines → WSL → Set up, or `wsl --install --no-distribution` first |
| all | Container engine | Podman or Docker is installed | Container mode is refused (`RUN-CTR-001`) |
| all | Running as root | not root or Administrator | a warning: services would run with those rights |
| all | Docker API on 2375 | nothing listens on `tcp://localhost:2375` | a warning: any service with network could control Docker |

## Speed on this machine

| Measure | Budget | How it's measured |
| --- | --- | --- |
| Sandbox start | ≤ 80 ms | build an empty jail and run `true` in it |
| Container start | ≤ 500 ms | start and remove a tiny container, when an engine is installed |
| Plugin start | ≤ 150 ms | from launch to a plugin's `hello` |
| Service, idle | ≤ 45 MB | resident memory of `pythonatord` |
| First paint | ≤ 400 ms | from launch to the window's first frame, reported by the window |
| Worst UI stall | ≤ 50 ms | how late the window's event loop ran, worst in each minute |

Measurements are kept for 30 days in `bench.json` and drawn as a sparkline, so you can see whether an update or a
change to the machine made things slower. The background service measures once a day; **Measure again** does it
now. A value over its budget is shown in amber.

**Copy for a bug report** puts the OS, the probes and the numbers on the clipboard.

## What would help on this machine

Only what applies here, each with the fix, for example:

* *No systemd user session*: memory and task limits fall back to the watchdog. **Copy:** `loginctl enable-linger $USER`
* *pasta isn't installed*: services with traffic can't start. **Copy:** `sudo apt install passt`
* *Sandbox start is over budget*: often a slow disk or antivirus scanning `/usr`. **Measure again** after a reboot.

When nothing would help, it says so.

The protection tables in [Isolation](isolation.md) come from the same data as System check's matrix
(`python -m pythonator.service.check --doc`), and a test fails when the two disagree.
