# Architecture

```text
          window (PyQt6)
                 │  QLocalSocket/TCP
                 ▼  mutual HMAC challenge-response
            pythonatord  ──────────────────────────────┐
      ┌──────────┼───────────────┬──────────────┐      │
      ▼          ▼               ▼              ▼      ▼
  SpecStore  Reconciler ──► policy compiler   LogStore  PluginHost
  (specs,    (every 1 s:     (spec + host ──►  (segments,  (one sandbox per
  approvals)  desired vs      argv + plan)     index)      plugin, grants)
              observed)
                 │
                 ▼
              Ledger (intend → done/undone) ──► bubblewrap · Seatbelt · AppContainer · podman/docker · wsl.exe
```

## The pieces

| Folder | What it does |
| --- | --- |
| `pythonator/model` | `ServiceSpec` (what you asked for) and `Observed` (what is happening). Strict pydantic models. |
| `pythonator/policy` | **The compiler.** `compile_service(spec, host)` is a pure function: it returns the exact argv and an `EnforcementPlan` of layers, or raises `Refused` with a code and a fix. `host.py` probes the machine once into `HostFacts`. `paths.py` holds the folder rules. `risk.py` computes the risk hash and the widenings. |
| `pythonator/jail` | `launcher.py` runs *inside* the Linux sandbox (standard library only): Landlock, the libseccomp filter, `no_new_privs`, then `exec`; exit 126 if any of it fails. `winjail.py` is the Windows launcher: AppContainer, job object, folder grants, Ctrl+C. `wslrun.py` and `wslsetup.py` run and install WSL mode's own distribution. |
| `pythonator/net` | The egress gate: checks each connection against the allowed hosts and records what it blocked. |
| `pythonator/work` | Work the daemon does for the window: the file tree and safe edits, private git snapshots, ruff in isolation, engines, Python installs with uv. |
| `pythonator/reconcile` | The `Reconciler`: every second (or when poked), makes observed match desired. Starts, stops, restarts with back-off, runs schedules, samples CPU and memory, and enforces the watchdog. |
| `pythonator/ledger` | Append-only JSONL with fsync: *intend* before any side effect, *done* or *undone* after. Read at start-up to recover. |
| `pythonator/logs` | `LogStore`: 4 MB segments, sparse index every 64 lines, range reads, masking before write. |
| `pythonator/store` | Atomic private writes (`0600`, fsync, rename), specs and approvals. |
| `pythonator/safe` | Safe file reading by descriptor (no links, no special files, size caps), the environment and trusted paths for host tools, the secret masker, start-up hardening and the exception hook. |
| `pythonator/plugins` | `PluginHost`: installs, isolates and talks to plugins; checks grants per call; runs `policy.check` hooks. |
| `pythonator/service` | `pythonatord` (`daemon.py`), the IPC protocol (`ipc.py`), the blocking client, and System check measurements. |
| `pythonator/ctl` | `pythonatorctl`: version and uninstall (plus the installers' `shutdown` and `service`). |
| `pythonator/ui` | The window. It never runs a service itself; every button is a request to `pythonatord`. |
| `pythonator/resources` | The icon, fonts, the plugin SDK, and the bundled plugins (Guard, Notify, AI assistant, Builder) with `official.json`. |

## Life of a service

1. **Apply.** The window sends `apply` with a spec. Enabled `policy.check` plugins review it (any refusal, crash or
   timeout refuses it). The spec is saved with a new generation. The compiler produces the plan and the risk hash.
2. **Approve.** You approve the plan; the risk hash is stored in `approvals.json`.
3. **Reconcile.** The desired state becomes *running*. The reconciler compiles again (the machine may have changed),
   checks the approval against the current hash, writes *intend* to the ledger, and starts the isolated environment.
4. **Run.** Output is pumped into the log store; `log` events tell subscribed windows the new line count, and the
   window reads only the lines on screen.
5. **Exit.** Back-off, give-up, or *finished*; scheduled services wait for their next time.

## The window

* `ui/link.py`: an asynchronous client (`QLocalSocket`, or TCP on Windows), so the window never blocks on the daemon. It starts the daemon if needed and warns when its version differs.
* `ui/console.py`: a `QAbstractScrollArea` that paints only visible lines from pages of 256 fetched by range.
* `ui/widgets.py`: painted delegates (layer rings, sparklines, chips, the security stack); no web views.
* `ui/theme.py`: the Cobra tokens from the logo, for dark and light, and the one stylesheet.

## Data folder

`~/.local/share/Pythonator` on Linux, `~/Library/Application Support/Pythonator` on macOS, `%LOCALAPPDATA%\Pythonator` on Windows (or `$PYTHONATOR_DATA`), owner-only:

| Path | Holds |
| --- | --- |
| `services/<name>.json` | specs (`.bak` keeps the previous one) |
| `approvals.json` | the risk hash you approved, per service |
| `ledger.jsonl` | what Pythonator started, for recovery |
| `logs/` | service logs; `logs/plugins/` plugin logs |
| `plugins/`, `plugins.json` | installed plugins; their grants, settings and hashes |
| `schedule.json` | last scheduled run per service |
| `bench.json` | System check measurements, 30 days |
| `ipc.key`, `run/` | the IPC key and socket (or port file) |
| `git/<name>.git`, `git/.no-hooks` | private snapshots of each service folder |
| `wsl/` | WSL mode's distribution (Windows) |
| `daemon.log`, `window.log` | uncaught errors, 1 MB each plus one `.1` |
