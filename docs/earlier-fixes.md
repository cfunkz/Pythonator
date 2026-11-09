# Earlier fixes: where each one is in 0.0.1

Every entry of the old app's CHANGELOG (0.0.1 to 0.0.19, Unreleased and the preview builds), with what 0.0.1 of the
rewrite does about it:

- **Built in**: the fix or feature is part of this design (where it lives, and the test when there is one).
- **Not relevant**: the feature or path that needed it doesn't exist here (and why).
- **Fixed now**: it was missing and was added while writing this list (with its test).
- **Still open**: true in this release too; listed in docs/isolation.md under Known limitations.

**Totals:** 142 built in · 105 not relevant · 9 fixed now · 1 still open · 257 entries.


## Unreleased

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 1 | Added | Windows: **WSL mode** runs a service in a WSL 2 Linux distribution, with Linux's own runtimes, through Microsoft's `wsl.exe` (`--distribution`, `--cd`, `--exec`). | **Not relevant** | No WSL direct mode and no `wsl` engine: WSL mode is the Sandbox environment inside Pythonator's own distro (`jail/wslrun.py`). |
| 2 | Added | Windows: Container mode's engine `wsl` runs containers with Docker Engine installed inside a WSL distribution, for computers without Docker Desktop, with the same hardening flags. | **Not relevant** | No WSL direct mode and no `wsl` engine: WSL mode is the Sandbox environment inside Pythonator's own distro (`jail/wslrun.py`). |
| 3 | Fixed | Container mode on Windows: Python ran `py` inside the container when there was no `.venv-container` yet, and Node.js without an entry file and the install steps ran `npm.cmd`. | **Not relevant** | Container mode runs the command as written inside the image; host program names are never substituted, and there are no install steps. |
| 4 | Fixed | Windows: closing the window while removing a service was still undoing its Make reachable (or while Make reachable or Open was still checking) raised an error in the event loop: the answer reached a part of the window… | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 5 | Fixed | git's timeout stopped counting once git itself ended, so a helper that kept its output open still held a pull. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 6 | Fixed | Full access on Linux and macOS: a child that left the service's process group (`setsid`) less than 2 s before Stop outlived Stop and Quit. | **Built in** | Full access on Linux runs in a systemd scope, so Stop ends the cgroup including setsid and double-fork children (`compile._limits_prefix`). Test: `test_4_setsid_and_double_fork_children_end_with_stop`. |
| 7 | Fixed | A plugin whose `plugin.toml` was a link to `/dev/zero` or a FIFO made the background service read until it ran out of memory, or wait for good, when the plugin was installed. | **Built in** | `plugin.toml` is read through safefs: regular file, non-blocking, 64 KB cap (`plugins/model.load_manifest`). Test: `tests/test_plugins.py::test_bad_manifest`. |
| 8 | Security | git pull: a sandboxed service could make a folder in its `.git` unreadable (`chmod 300`). | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). gc and maintenance are off on every git call anyway. |
| 9 | Security | git pull: a service could delete its own `.git/HEAD`, so git skipped that `.git` and used the repository around the service's folder, fetching into it and fast-forwarding it. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). `GIT_DIR` and `GIT_WORK_TREE` are set on every call. |
| 10 | Security | Linux: Landlock doesn't cover a file's owner or mode. | **Built in** | seccomp refuses chmod adding setuid, setgid or o+w; openat2 ENOSYS; by-number filters for old libseccomp (`jail/launcher.py`). |
| 11 | Known limitations | A service that crashes *Give up after* times in 10 minutes (5 by default) stays stopped until you start it. | **Fixed now** | Giving up is followed by one more try after 10 minutes (`reconciler.COOL_DOWN_S`). Test: `test_giving_up_is_followed_by_one_more_try_after_a_cool_down`. |
| 12 | Known limitations | Linux without bubblewrap: `/dev/shm` and other programs' System V shared memory are shared with the service, and before Linux 6.12 it can send signals to any of your processes, the background service included. | **Not relevant** | Linux Sandbox always uses bubblewrap: /dev/shm and System V IPC are private and the PID namespace hides other processes. |
| 13 | Known limitations | Linux and macOS: memory and CPU limits are watched by Pythonator every 2 s, not enforced by the kernel, and there's no limit on the number of processes. | **Built in** | Linux uses cgroup limits through systemd-run (TasksMax caps processes); the watchdog is only the labelled fallback; plugins have a 256 MB watchdog. |
| 14 | Known limitations | Linux without bubblewrap: a process a plugin started can outlive Disable. | **Not relevant** | Plugins always run under bubblewrap on Linux; Disable ends the PID namespace. |
| 15 | Known limitations | Sandbox and Container modes still accept `/tmp`, `/opt`, `/var`, `/srv`, `/run/user`, user site-packages and folders on your shell's `PATH` that aren't on Pythonator's own `PATH` (`~/.cargo/bin`, `~/go/bin`, a `~/.loc… | **Fixed now** | /opt, /srv, /nix, /snap, /tmp itself, user site-packages and PATH folders inside home are refused (`policy/paths.py`). Test: `test_new_deny_list_entries`. |
| 16 | Known limitations | A sandboxed service can make files in its folder readable by other accounts. | **Still open** | Still a known limitation: seccomp refuses a chmod that adds write for others, but not read for others. Listed in docs/isolation.md. |
| 17 | Known limitations | A sandboxed Rust service can read `~/.cargo` (including `credentials.toml`). | **Fixed now** | Runtime grants stop at ~/.cargo/bin; rustup's toolchains come from ~/.rustup (`policy/runtimes.py`). Tests: `test_runtime_grants_never_cover_a_dot_folder_parent`, `test_rustup_cargo_sees_its_toolchains_but_not_cargo_credentials`. |
| 18 | Known limitations | A clone runs in the window, outside the lock a pull takes, and a checkout's `.lfsconfig` can redirect git-lfs downloads when git-lfs is installed globally. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 19 | Known limitations | Nothing restarts a service whose health check fails, because there are no health checks yet. | **Built in** | HTTP and TCP health checks restart a service that stops answering (`model/service.Health`, `reconciler._probe`). |

## 0.0.19

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 20 | Added | A start refused because a port is in use or reserved by Windows (Hyper-V, WSL, Docker) names a free port near it. | **Built in** | NET-PORT-001 names a free port nearby before a start (`reconciler._busy_port`, `_free_near`). Test: `test_busy_port_is_named_before_the_start`. Make reachable doesn't exist. |
| 21 | Added | A running server gets a note when it listens on IPv6 only (`[::1]`, what `localhost` can mean), since the Open button, Make reachable and `HOST` use `127.0.0.1`, and when another program listens on the same port and m… | **Not relevant** | No [::1]-only note: `HOST` is set from the approved reach, so a server binds where its port is published. |
| 22 | Changed | Container mode's default images: Deno `denoland/deno:debian` (the `2` tag never existed, so Deno couldn't start), Go `golang:1.27` (1.24 has ended), .NET `sdk:10.0` (9.0 ends on 2026-11-10), Node.js `node:24-slim`. | **Built in** | Default images are checked weekly with `docker manifest inspect` (`.github/workflows/security.yml`). |
| 23 | Changed | Container mode keeps Go, Rust, Ruby, Deno and NuGet packages in the project's `.container-cache` folder, so installs work with the read-only image. | **Not relevant** | Pythonator runs no package installs in 0.0.1. Container home is a tmpfs and the project is writable. |
| 24 | Changed | Project templates ask for supported runtimes: Node.js 22, Python 3.11, Go 1.26, Ruby 3.3, PHP 8.3, Java 21. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 25 | Changed | The discord.py, discord.js and grammY bot templates log out cleanly when stopped on Linux and macOS (SIGTERM). | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 26 | Changed | The file tree shows `node_modules`, `.venv`, `venv`, `vendor`, `.git`, `target` and similar folders without listing them: a big `node_modules` held the window for over a second each time it was opened. | **Built in** | The file tree hides heavy folders and lists at most 3,000 entries (`work/files.py`). |
| 27 | Changed | The file tree no longer watches the folder. | **Built in** | No folder watching; the tree is read on demand by the daemon. |
| 28 | Changed | A pull needs the service's **Git** address in its settings, and fetches only that address and branch. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 29 | Changed | Release builds use Python 3.14. | **Built in** | CI uses setup-python with `check-latest: true` (`.github/workflows/`). |
| 30 | Fixed | A crashed service whose child still held its output (a worker pool, an ffmpeg) showed as running and was never restarted. | **Built in** | The main program's exit is the exit; readers get 2 s, then the group is killed (`reconciler._wait`). |
| 31 | Fixed | An error that nothing caught, anywhere in the window or the background service, ended the whole program (PyQt's default). | **Built in** | Global excepthook in both processes (`safe/process.install_hook`). Test: `test_14_exception_in_a_slot_keeps_the_app_alive`. |
| 32 | Fixed | System check: long paths wrap instead of pushing the dialog past its window, the note at the bottom wraps, and paths in your home folder show as `~` (they showed your user name). | **Built in** | System check wraps and shows `~` for home; the copied report replaces home with `~` (`ui/syscheck.py`). |
| 33 | Fixed | Ending a plugin ends every process it started: its process group, and on Windows its own job object (the sandboxed worker outlived the launcher before). | **Built in** | Plugins run in a bubblewrap PID namespace (Linux) or a job (Windows); Disable ends the tree. |
| 34 | Fixed | Linux: in Docker or LXC, where bubblewrap can make a user namespace but not mount `/proc`, every sandboxed start and plugin failed. | **Built in** | System check probes a real sandbox; bubblewrap that can't mount /proc refuses Sandbox with the reason (`service/check.py`). |
| 35 | Fixed | A server with forked workers (gunicorn, uvicorn `--workers`, PHP-FPM, Node.js cluster) was stopped far below its memory limit: the memory the workers share with their parent was counted once per worker (300 MB with fo… | **Built in** | The watchdog acts on private bytes (PSS/USS), not summed RSS (`reconcile/proc.private_bytes`). |
| 36 | Fixed | The file tree's icon provider could be freed before the tree's model, which still pointed at it; the next event crashed (macOS CI). | **Not relevant** | No file-system model or icon provider: the tree is a plain list from the daemon. |
| 37 | Fixed | A memory or CPU limit typed with hundreds of digits raised an error instead of saying what to write. | **Built in** | Limits are bounded integers in the model; the error names the field (`model/service.Limits`). |
| 38 | Fixed | A plugin disabled for misbehaving: the messages it had already sent are dropped. | **Built in** | Once a plugin is killed its reader task returns; queued messages are never acted on (`plugins/host._reader`). |
| 39 | Fixed | Linux under bubblewrap: programs that open a terminal (`pty.fork`, `forkpty`) work again. | **Built in** | Sandbox always runs under bubblewrap and there is no setsid seccomp rule, so pty.fork works. |
| 40 | Fixed | Opening a FIFO or other special file from the tree or the Open dialog froze the window for good; it is refused. | **Built in** | Files are read through safefs (regular, non-blocking, 2 MB cap) (`work/files.read`). Test: `tests/test_safe.py::test_fifo_never_blocks`. |
| 41 | Fixed | An error on a background thread of the window showed its status line from that thread, which Qt doesn't allow. | **Built in** | Status messages from threads go through a queued Qt signal (`ui/app._Bridge`). |
| 42 | Fixed | git's timeout started only after git closed its output, so a host that never answered held a pull, and a plugin installed from git held the background service, forever. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 43 | Security | Linux: nothing a sandboxed service starts can leave its process group. | **Built in** | Linux Sandbox uses a PID namespace, which ends every child; no setsid rule is needed. |
| 44 | Security | git: `GIT_ALLOW_PROTOCOL=https:ssh`. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 45 | Security | A pull also waits for, or refuses while, a sandboxed service that may write the checkout through *More folders*. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 46 | Security | Official plugins no longer run without a sandbox where it can't be set up (0.0.18 ran them with your full rights on Linux with Landlock but neither libseccomp nor bubblewrap). | **Built in** | Plugins never start without their isolated environment (PLG-LNX-001/002, TOOL-*). Test: `test_plugin_jail_and_grants`. |
| 47 | Security | Windows: WSL's `bash.exe` and `wsl.exe` in System32 are refused in Sandbox mode. | **Built in** | RUN-WIN-003 refuses wsl, bash and WSL helpers in Sandbox mode (`compile._windows_command`). |
| 48 | Security | Files a sandboxed service can write in its folder and Pythonator reads itself (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `composer.json`, `pyvenv.cfg`, the env file, and files plugins read): only regul… | **Built in** | Every read of a service folder goes through safefs. Test: `test_1_only_safefs_reads_service_folders`. |
| 49 | Security | Release builds install Pythonator with `--no-build-isolation`, so setuptools comes from the hash-locked build lock instead of an unchecked download. | **Built in** | The release builds with `--no-build-isolation` and a hash-locked setuptools (`requirements-build.lock`, `release.yml`). |
| 50 | Security | git pull: a sandboxed or container service could put symbolic links in its own `.git`. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 51 | Security | git pull: the service could change its checkout's `origin` and have the pull fetch any repository your SSH key or credential helper can read into its folder. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 52 | Security | Container mode refuses the same folders as Sandbox mode: your home folder, Pythonator's data folder and folders of keys. | **Built in** | Container folders pass the same deny list. Test: `test_folder_denied`. |
| 53 | Security | Sandbox and Container modes also refuse, as a folder or a write grant: folders on `PATH` (Pythonator runs git, docker and the runtimes from there, as you), Pythonator's own program folder and the Python it runs on, lo… | **Fixed now** | PATH folders inside home are refused as write folders (POL-PATH-012) and the program folder too (POL-PATH-011). Test: `test_pythonators_own_program_folder_is_refused`. |
| 54 | Security | Sandboxed services and plugins no longer inherit more variables that usually hold secrets: names with `PASS`, `AUTH`, `ACCESS_KEY`, `DSN`, `SESSION`, `COOKIE`, `WEBHOOK` or `DATABASE_URL`, and any value that is an add… | **Built in** | Nothing is inherited: isolates and plugins get a fixed environment. Test: `test_2_poisoned_environment_never_reaches_a_host_tool_or_jail`. |
| 55 | Security | Release: a tag only publishes when it is on `main` and CI and Security passed on that exact commit. | **Built in** | The release gate checks tag on main, tag = version, notes present; required checks guard main (`release.yml`). |
| 56 | Known limitations | A sandboxed service can connect to servers on this computer (`127.0.0.1`): a database, Redis, Ollama, the Docker API over TCP. | **Built in** | Linux pasta `--no-map-gw`, macOS Seatbelt and the Windows AppContainer block 127.0.0.1. Test: `test_macos_blocks_loopback_with_traffic_on`. |
| 57 | Known limitations | With no ports listed, Linux and macOS don't limit which ports a sandboxed service listens on. | **Built in** | Linux services have their own network namespace and macOS allows bind only on listed ports. |
| 58 | Known limitations | macOS: a program that starts a new session and clears its environment can outlive Stop. | **Built in** | Still true for macOS and listed in docs/isolation.md Known limitations. |
| 59 | Known limitations | Nothing restarts the background service if it dies with no window open. | **Built in** | `pythonatorctl service install` adds a systemd user unit, LaunchAgent or logon task; docs say Windows services end with the daemon. |
| 60 | Known limitations | Linux limits are watched by Pythonator, not enforced by the kernel (no cgroups yet). | **Built in** | Linux limits are cgroups via systemd-run; the watchdog is the labelled fallback. |
| 61 | Known limitations | Releases aren't code signed or notarized. | **Built in** | Still true; docs/isolation.md says so and the installers never suggest bypassing SmartScreen or Gatekeeper. |
| 62 | Known limitations | Windows: another local account could create the background service's pipe first (a shared PC only). | **Built in** | No pipe; challenge-response means a squatter learns nothing. Test: `test_ping_and_wrong_key_is_refused[tcp]`. |

## 0.0.18

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 63 | Added | AI assistant: an answer that lays out a project (a changed `main.py`, a new `static/index.html`, a new `static/js/game.js`) is one **Review changes to 3 files** button and one review window that lists every file with … | **Not relevant** | The AI plugin's answers become exact-text edits shown as a diff; Pythonator applies only what you click (`work/files.apply_edit`). |
| 64 | Changed | Linux: Sandbox mode refuses to start when neither libseccomp nor bubblewrap is available. | **Built in** | bubblewrap is required; no Landlock-only Sandbox. Test: `test_missing_bwrap_refuses`. |
| 65 | Changed | Schedules: a cron line must have five fields (minute, hour, day of month, month, day of week). | **Built in** | Cron takes exactly five fields (`model/service.Schedule`). |
| 66 | Changed | Project builder: new projects start from Fastify 5.12.8, Hono 4.13.16, @hono/node-server 2.1.6, Elysia 1.4.31, Sinatra 4.2 and Puma 8.0.2 or later. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 67 | Changed | Dependency floors raised to the versions CI tests: psutil 7.2 (and below 8, which changes `Process.info`), pydantic 2.14, platformdirs 4.12, setuptools 84.1 for building, pytest 9.0.3 for development. | **Fixed now** | Major caps added: psutil<8, pydantic<3, PyQt6<7 and the rest (`pyproject.toml`). |
| 68 | Changed | CI: the test job runs on Ubuntu 24.04 by name, with Ubuntu 26.04 as a job of its own. | **Built in** | CI runs Ubuntu 24.04 by name and uses attest-build-provenance (`.github/workflows/`). |
| 69 | Changed | A weekly workflow, **Refresh locks**, compiles every `*.lock` file again with `--upgrade`, checks the result with pip-audit, ruff, mypy and the tests, and opens one pull request. | **Not relevant** | One lock file regenerated with `uv pip compile --generate-hashes`; Dependabot with a 7-day cooldown and weekly pip-audit replace the refresh workflow. |
| 70 | Changed | `.env` and `.env.*` files are ignored by git in this repository (`.env.example` is not). | **Fixed now** | `.env` and `.env.*` added to `.gitignore`. |
| 71 | Fixed | Project builder: Express 5 apps (Express API, WhatsApp bot) exit with an error when their port is taken. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 72 | Fixed | Plugins: their messages are read from the pipe in blocks. | **Built in** | Plugin pipes are asyncio buffered readers (`plugins/host._reader`). |
| 73 | Fixed | Windows: closing the window while Make reachable, or its undo after a removal, was still running no longer raises an error in that thread. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 74 | Security | Windows: Make reachable, its undo, and the firewall-rule check start PowerShell by its full path in System32. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 75 | Security | Git pull: a pull (manual or on start) no longer runs while another service in the same folder, inside it or around it, is running, starting or installing in any mode but Full access, nor while one of their console com… | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 76 | Security | Plugins (and so the AI provider and Notify's webhooks) get service output and notes with every value of the service's variables and env file masked, whatever its name, plus the password inside a URL. | **Built in** | Everything for plugins gets the `everything=True` mask. Test: `test_11_secret_values_are_masked_for_every_channel`. |

## 0.0.17

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 77 | Fixed | AI assistant: an answer that writes a whole file (nothing before `=======`) for a file that exists but is empty, such as a new project's `main.py`, is written after you review it. | **Not relevant** | The AI plugin's answers become exact-text edits shown as a diff; Pythonator applies only what you click (`work/files.apply_edit`). |

## 0.0.16

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 78 | Added | Single instance per data folder on Windows, macOS and Linux: starting Pythonator again brings the open window to the front, and a start at sign-in exits quietly. | **Built in** | One daemon per data folder; any number of windows share it (`daemon.serve`). |
| 79 | Added | System check: **Left behind by services** lists anything that can keep a port open or a firewall rule in place after a service has gone, and **Clean up…** removes it. | **Built in** | Leftovers are in the ledger and the Plan page offers Heal (`reconciler.drift`). |
| 80 | Added | Linux: the sandbox launcher is a child subreaper (`PR_SET_CHILD_SUBREAPER`), so Stop also ends processes that left the service's process group (`setsid`, double fork). | **Not relevant** | Linux Sandbox uses a PID namespace instead of a subreaper; Full access uses a cgroup. |
| 81 | Added | Project builder 1.4.0: the setting **Refresh looks for leftover project folders** (off by default) lists unused folders in the projects folder, and **Clean up N leftover folders…** deletes the selected ones after conf… | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). Pythonator never deletes folders. |
| 82 | Added | Right-click **Remove…** on a Project builder project offers **Delete project and folder** or **Remove service, keep folder**, as the builder's own Delete does. | **Not relevant** | Pythonator never deletes service folders. |
| 83 | Added | Editor: saving over a file that changed on disk since it was opened asks for confirmation. | **Built in** | Saving checks the hash from when the file was opened (FILE-007). Test: `test_write_refuses_stale_hash_and_escapes`. |
| 84 | Changed | Service settings: memory and CPU limits moved to a new **Limits** tab. | **Built in** | Settings has Limits and Schedule tabs per the wireframes (`ui/settings.py`). |
| 85 | Changed | Service settings: schedules moved from General to a new **Schedule & restarts** tab, alongside the restart rules. | **Built in** | Same: Schedule & restarts tab. |
| 86 | Changed | **F5** only starts a service; **Ctrl+R** restarts. | **Not relevant** | New key map from the command palette; Start, Stop and Restart are buttons and palette entries. |
| 87 | Changed | Plugin settings: labels wrap onto as many lines as needed, and yes/no settings are a check box with the label beside it. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 88 | Changed | AI assistant 1.3.1: edit blocks are parsed in the forms models actually produce (missing `>>>>>>> END`, `>>>>>>> REPLACE`, blocks inside code fences, `<<<<<<< SEARCH` under the file name, CRLF). | **Not relevant** | The AI plugin's answers become exact-text edits shown as a diff; Pythonator applies only what you click (`work/files.apply_edit`). |
| 89 | Changed | Messages to plugins are written by one thread per plugin with a 2,000-message queue; a plugin that falls behind is stopped and disabled. | **Built in** | Plugin writes are queued with a 2 MB backlog cap; a plugin that falls behind is stopped and disabled (`plugins/host._write`). |
| 90 | Changed | Typed input to a service is written by its own thread; more than 256 unread lines are refused. | **Built in** | Stdin refuses more than 64 KB buffered, with a 2 s drain timeout (`reconciler.send_stdin`). |
| 91 | Changed | Container services are prepared and started off the background service's main thread. | **Built in** | Container starts are subprocesses on the event loop, never blocking it. |
| 92 | Changed | Stats: one process-table pass and one connections call per round for all services, instead of per service (previously 17.7 ms per service with 600 processes). | **Built in** | One stats sample per service per second; no system-wide connection scans. |
| 93 | Changed | Console search highlights matches on screen only, after typing pauses, and counts the rest (previously 353 ms per keystroke on a long log). | **Built in** | Search runs in the daemon over the log store; the console draws only visible rows. |
| 94 | Changed | Project builder 1.4.0: takes service state from events instead of reloading all templates and projects on every start and stop; template checks run off the main loop with one lookup per tool. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 95 | Changed | Opening a file reads only its first 8 KB to detect the encoding. | **Built in** | Files are read once through safefs and decoded as UTF-8 with replacement. |
| 96 | Removed | Unused code: `git.version()` and `MIN_VERSION`, `Supervisor._pids`, `launcher.windows_profile_sid` and `windows_acl_sids`, `FileTree.status`, `MainWindow.header`, `ansi.strip`, `PluginHost._write_raw`, `NODE_OPTIONS_P… | **Not relevant** | Old code paths, not carried over. |
| 97 | Fixed | Windows: removing a service left its Make reachable firewall rules, loopback exemption and loopback permit in place. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 98 | Fixed | A plugin that stopped reading its messages froze the background service. | **Built in** | Plugin backlog cap stops and disables a plugin that isn't reading. |
| 99 | Fixed | Typing into a service that does not read its input blocked the background service (from 131 KB on Linux, about 4 KB on Windows). | **Built in** | Stdin is bounded and drained with a timeout. |
| 100 | Fixed | A slow Docker or Podman (for example, Docker Desktop still starting) stalled all services and the window for up to about 90 s. | **Built in** | Engine calls are async subprocesses with timeouts. |
| 101 | Fixed | Stop or Remove during pull-on-start still launched the service; a double Start created two runners. | **Built in** | Stop marks the run stopping first; a second Start can't create a second runner (`reconciler.tick`). |
| 102 | Fixed | Closing a modified tab with **Save** when the save failed closed the tab and lost the edits. | **Built in** | A failed save keeps the tab and shows the error (`ui/editor.py`). |
| 103 | Fixed | A new background service exited while the previous one was shutting down, leaving the window on "connecting". | **Built in** | A second daemon exits with "already running"; the window reconnects (`ui/link.py`). |
| 104 | Fixed | Two windows or two background services could run at once, so every question was asked twice and every service ran twice. | **Built in** | One daemon per data folder. |
| 105 | Fixed | Removing a service a second time (a second click or window, while it was stopping or after it had gone) force-killed it or showed "No such service". | **Built in** | Remove is idempotent (`reconciler.remove`). |
| 106 | Fixed | Windows: Project builder **Refresh** could offer a leftover folder again while it was being deleted. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 107 | Fixed | AI assistant: a missing `>>>>>>> END` wrote the next edit block's markers into the file. | **Not relevant** | The AI plugin's answers become exact-text edits shown as a diff; Pythonator applies only what you click (`work/files.apply_edit`). |
| 108 | Fixed | Notify 1.1.1: a `Retry-After` header containing a date stopped alert delivery; HTTPS webhooks could redirect to HTTP. | **Built in** | Notify plugin requests go through the net proxy: https only, no redirects followed (`plugins/netproxy.py`). |
| 109 | Fixed | Windows: replacing the private Node.js copy no longer removes one that a running service is still using. | **Not relevant** | No private Node.js copy, preload or libuv patch; Node gets a read-only grant on its install root. |
| 110 | Fixed | White ANSI text was invisible on the light theme. | **Not relevant** | No ANSI colour rendering in the console in 0.0.1. |
| 111 | Fixed | Header icons are redrawn after a display scaling change. | **Built in** | Icons are drawn from SVG per device pixel ratio (`ui/icons.py`). |
| 112 | Fixed | A plugin name longer than 64 characters is truncated in API errors. | **Built in** | Plugin names are capped in the manifest model (`plugins/model.py`). |
| 113 | Security | Git pull: a checkout whose `.git/config` holds settings outside an allow-list (`core.sshCommand`, filter drivers, `credential.helper`, `include.path` and others) is refused, naming each setting. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 114 | Security | The Python version check before a start reads `pyvenv.cfg` instead of running the project's `.venv` Python with `--version` outside the sandbox. | **Built in** | Versions come from the runtime table; nothing runs from the service folder. Test: `test_1_only_safefs_reads_service_folders`. |
| 115 | Security | Sandbox read grants outside the project come only from a real Python installation. | **Built in** | Grants come from the spec and the runtime table only (`policy/compile._roots`). |
| 116 | Security | Env files are read only when they resolve inside the project folder (a link to `~/.aws/credentials` previously loaded the keys). | **Not relevant** | No env files. |
| 117 | Security | Inherited environment variables named like secrets (`GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, `*_API_KEY`) are no longer passed to sandboxed services or plugins. | **Built in** | Nothing is inherited by isolates or plugins. |
| 118 | Security | Sandbox **More folders**: a folder that contains a credentials folder (for example `~/.config`, which holds `gh` and `gcloud`) is refused. | **Built in** | A folder that contains a secret folder is refused (`policy/paths.py`). Test: `test_new_deny_list_entries`. |
| 119 | Security | Linux: Landlock-only sandboxes can read only the required device files (no cameras, microphones or `/dev/pts`); `/dev/pts` is available only inside bubblewrap. | **Built in** | bubblewrap's minimal /dev only. |
| 120 | Security | macOS: the Seatbelt profile no longer allows `/dev/ttys*`. | **Built in** | Seatbelt allows only /dev literals (`policy/seatbelt.py`). |
| 121 | Security | Compose: named volumes whose `driver_opts` bind a host folder outside the project, and networks named or driven by `host`, are refused. | **Not relevant** | No Compose in 0.0.1; one hardened container per service. |
| 122 | Security | Container mode refuses a project path containing a comma or quote, which could add a second mount source. | **Built in** | RUN-CTR-002 refuses `,`, `:`, quotes and newlines. Test: `test_container_mount_options_cant_be_smuggled_in_a_folder_name`. |
| 123 | Security | Plugins: `read_files` and `propose_changes` refuse any path through a symbolic link (a link to `id_rsa` or `.git` was followed). | **Built in** | Plugin file access goes through safefs with no links. Test: `test_links_are_refused`. |
| 124 | Security | Plugins: removing a plugin, or installing different code under the same ID, clears its grants and live mode. | **Built in** | Grants are tied to the content hash; new code starts with none. Test: `test_new_code_resets_grants_and_marks_new_permissions`. |
| 125 | Security | Plugins: rate limits by size as well as count (2 MB/s), 50 stderr lines per second, 2,000 characters per log line, and one `hello` per process (a second one re-read the keychain). | **Fixed now** | 200 messages/s, 1 MB per message, plus 50 stderr lines/s and a 2 MB/s stderr budget added now (`plugins/host._stderr`). |
| 126 | Security | Plugin, program and git text is shown as plain text in labels, message boxes and tooltips. | **Fixed now** | Every tooltip is now shown as plain text by an app-wide filter (`ui/app._PlainTips`); labels were already plain or escaped. |
| 127 | Security | Windows: a process that cannot be added to its job object is never resumed. | **Built in** | A process that can't join its job is never resumed (`jail/winjail.py`). |
| 128 | Security | The background service's token file is written only once it is listening. | **Built in** | Challenge-response: the key never crosses the socket, and the port file appears only after the server listens. |

## 0.0.15

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 129 | Added | Project builder 1.3.0: 38 templates and a **Runtime** filter. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 130 | Added | Runtime version checks before a start, for every runtime, from the template's requirements and the project's `package.json` engines, `requires-python`, `go.mod`, `rust-version` and Composer's `php`. | **Not relevant** | No runtime version floors per template; System check shows the runtime versions found. |
| 131 | Added | A start reports a listed port that is already in use (and by which program) or, on Windows, reserved for Hyper-V, WSL or Docker. | **Built in** | NET-PORT-001 names the busy port and a free one nearby. |
| 132 | Added | Windows: a start reports when a sandboxed server is not yet reachable from the same PC, and **Open** offers **Make reachable** first. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 133 | Added | CI starts a Vite app in the Windows sandbox on every push, makes it reachable and loads its page. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 134 | Changed | The plugin dock is laid out once resizing or moving stops (97 ms to 5 ms per drag step with a long chat). | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 135 | Changed | The console shows the command that actually runs, including the private Node.js copy in the Windows sandbox. | **Built in** | The plan's first line is the first console line and states what runs (`reconciler._start`). |
| 136 | Changed | Nuxt runs with `--no-fork`, Astro's dev server stays in the foreground, and Astro's starter is generated locally instead of being downloaded from GitHub. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 137 | Fixed | Windows sandbox: Node.js hung at 100% of a core when a program started a child process (Vite, Next.js, Angular, npm install scripts). | **Not relevant** | No private Node.js copy, preload or libuv patch; Node gets a read-only grant on its install root. |
| 138 | Fixed | Windows sandbox: Vite stopped at *EPERM: operation not permitted, realpath*; the Node.js preload now answers with the existing path when Windows refuses `realpath`. | **Not relevant** | No private Node.js copy, preload or libuv patch; Node gets a read-only grant on its install root. |
| 139 | Fixed | Linux sandbox: services with listed ports may also bind a system-chosen port (port 0), which Next.js Turbopack and Nuxt workers need. | **Built in** | Linux services have their own network namespace, so any port (including 0) binds inside it. |
| 140 | Fixed | Rust installed with rustup works in the sandbox (toolchains folder readable, `RUSTUP_HOME` set). | **Fixed now** | rustup's toolchains folder is readable and `RUSTUP_HOME` is set (`policy/runtimes.rustup_home`). Test: `test_rustup_cargo_sees_its_toolchains_but_not_cargo_credentials`. |
| 141 | Fixed | Nuxt failed with Node.js 22.13; Nuxt 4 needs 22.22.3 or newer, which is now reported before the start. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |

## 0.0.14

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 142 | Changed | Revoking a sandbox's folder permissions after Remove or Delete runs in the background and skips a folder that is about to be deleted (previously minutes for a large `node_modules` on Windows). | **Built in** | Grants are revoked off the event loop by the reconciler (`reconciler._revoke_grants`). |
| 143 | Changed | The first sandboxed Node.js start copies Node.js in the background, with a console note. | **Not relevant** | No private Node.js copy, preload or libuv patch; Node gets a read-only grant on its install root. |
| 144 | Changed | Project builder 1.2.1: a single **Create** button; an empty name uses the suggested one. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 145 | Fixed | Windows sandbox: npm and Node.js failed with *EPERM: operation not permitted, lstat 'C:\'* (create-vite, create-vue and other Node.js templates). | **Not relevant** | No private Node.js copy, preload or libuv patch; Node gets a read-only grant on its install root. |
| 146 | Fixed | Windows: deleting a project now deletes its folder. | **Not relevant** | Pythonator never deletes service folders. |
| 147 | Fixed | Windows CI tests the **Let sandboxes use it** check as it has worked since 0.0.13. | **Not relevant** | No Let sandboxes use it: grants are per service and recorded in the ledger. |

## 0.0.13

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 148 | Added | Plugin API 5: views (`ui.tabs`) and icon buttons (`ui.button(..., icon="refresh")`). | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 149 | Added | Notify 1.1.0: **Services** and **Events** views. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 150 | Added | Header **Details (i)**: process, CPU, memory, ports, isolation, restart policy and folder; selecting a line copies it. | **Built in** | The Plan page shows process, limits, folders and isolation per the wireframes (`ui/plan.py`). |
| 151 | Changed | Plugin panels cannot exceed the dock: text and paths wrap, buttons are elided with "…", drop-downs fill the row, and the dock is at least 240 px wide. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 152 | Changed | The header is a single line of icon buttons (Start, Stop, Restart, Packages, Configure, Details, More). | **Built in** | The header follows the wireframes (`ui/service.py`). |
| 153 | Changed | The editor no longer has its own toolbar (New, Open and Save sit beside its tabs); tabs are lower, and the console uses icon buttons. | **Built in** | The editor follows the wireframes (`ui/editor.py`). |
| 154 | Fixed | Windows sandbox: Node.js projects failed with "Access is denied" when Node.js was installed for all users in `C:\Program Files\nodejs` (the 0.0.12 check never triggered). | **Built in** | Node in Program Files gets a read-only grant on its install root (`policy/runtimes.install_root`). |
| 155 | Fixed | AI assistant: edits for the prompt's example path (`path/to/file.py`) or the wrong folder go to the one file that contains the replaced text; trailing spaces are ignored when matching; an edit that matches no file is … | **Not relevant** | The AI plugin's answers become exact-text edits shown as a diff; Pythonator applies only what you click (`work/files.apply_edit`). |

## 0.0.12

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 156 | Added | Project builder: Discord, Telegram and WhatsApp bots in Python or Node.js (discord.py, discord.js, python-telegram-bot, grammY, and a signature-checked WhatsApp Cloud API webhook), an Express API and a Python worker; … | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 157 | Added | Windows: **System check** offers **Let sandboxes use it…** for a runtime folder that sandboxes cannot read; one administrator prompt grants read and execute. | **Not relevant** | No elevated Let sandboxes use it; grants need no admin rights. |
| 158 | Added | The CPU and RAM meter shows Pythonator's own share (window, background service, plugins and services). | **Not relevant** | Not in 0.0.1 (not in the wireframes). The status bar shows the daemon, plugins and engines instead. |
| 159 | Changed | Plugin panels are tabs in a single Plugins dock again, with no separate windows or layout lock. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 160 | Changed | Panel rows of cards, lists or buttons wrap; wide tables scroll horizontally. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 161 | Changed | Templates: Django 5.2 LTS (5.2.19 or newer), Flask 3.1.3 or newer, FastAPI 0.162 or newer. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 162 | Fixed | The console stuttered while the window was resized or moved (since 0.0.11). | **Built in** | The console is virtual and doesn't redraw on resize. |
| 163 | Fixed | AI answers were cut off at the bottom of their bubble, and the note under a question overlapped it. | **Built in** | The AI view follows the wireframes. |
| 164 | Fixed | Windows: Node.js in `C:\Program Files\nodejs` without the sandbox read entry failed with "Access is denied"; the start now reports it. | **Built in** | Node in Program Files is granted read-only, so it runs. |
| 165 | Fixed | The Plugins window stopped long permission descriptions and setting names from wrapping. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 166 | Fixed | Docker Compose on Windows: Docker Desktop's Compose plugin was not found; an old or missing Compose is now reported. | **Not relevant** | No Compose in 0.0.1; one hardened container per service. |

## 0.0.11

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 167 | Added | Notify plugin, replacing Health checks and Webhooks (settings migrate automatically): a single timeline of crashes, abandoned restarts, blocked starts, failed installs, failing health checks, recoveries and anomalies … | **Built in** | The Notify plugin keeps a timeline of crashes, give-ups and blocks (`resources/plugins/notify`). |
| 168 | Added | Project builder plugin: React, Vue, Nuxt, FastAPI, Flask or Django projects created by each framework's own tool in the new service's sandbox, added on a free local port. | **Not relevant** | The old Project builder templates are gone; the Builder plugin has four small built-in templates (`resources/plugins/builder`). |
| 169 | Added | Docker Compose projects as a single service in Container mode: `compose up`, `compose stop`, and **Compose ▾** for build, pull, ps and down. | **Not relevant** | No Compose in 0.0.1; one hardened container per service. |
| 170 | Added | Plugin panels as docks, tabbed or floating, with a remembered layout. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 171 | Added | Service window: **＋ Add** and **− Delete** in the Environment and Folders tables, and **Import from env file**. | **Built in** | Settings → Variables adds and removes rows; no env-file import (no env files). |
| 172 | Added | **System check** redesigned: system, current load, installed runtimes and engines with versions, and enforced sandbox protections. | **Built in** | System check per the wireframes; the copied report omits paths beyond `~`. |
| 173 | Changed | AI assistant 1.2.0: complete answers rendered as Markdown (plugin API 4). | **Built in** | Plugin messages may be up to 1 MB; AI answers are plain text. |
| 174 | Changed | Panel text no longer re-wraps on every pixel of a resize; plugin tables update in place; the console does not redraw while the window is dragged or resized. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 175 | Fixed | Editor: saving added blank lines to CRLF files; pasted text now keeps the file's line endings; Undo straight after opening emptied the file. | **Built in** | The editor saves the text as it is; line endings aren't converted (`work/files.write`). |
| 176 | Fixed | Windows: a plugin showed "Unhandled exception in script" (`BrokenPipeError`) when closed or restarted. | **Built in** | Plugins run under the daemon's own sandbox; no window-side plugin process. |

## 0.0.10

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 177 | Added | Editor find bar: match counter, highlighting of all matches, match case, whole word and regular expressions, and a replace row (Replace all is a single undo step). | **Not relevant** | Not in 0.0.1 (not in the wireframes). The editor has highlighting, problems and AI fixes. |
| 178 | Added | AI assistant 1.1.0: chat view with streamed answers, code blocks and concise errors; a **Model** list per service, fetched, cached for a day and refreshed with **↻ Refresh**, which also tests the connection. | **Built in** | The AI view per the wireframes, provider calls through the net proxy. |
| 179 | Added | Plugin API 3: chat panels (`ui.chat`, `ui.message`). | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 180 | Fixed | AI assistant: a misspelt model name is reported instead of failing with a 404; Gemini errors are readable. | **Built in** | The AI plugin reports provider errors as text. |

## 0.0.9

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 181 | Fixed | Windows: uninstalling ends every Pythonator process still running from the install folder, so `Pythonator.exe` can always be deleted. | **Built in** | `install.ps1 -Uninstall` runs `pythonatorctl shutdown --uninstall` first. |

## 0.0.8

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 182 | Added | Console input switch **stdin / shell** (Ctrl+\`): shell mode runs a command in the service's folder, sandbox or container, with its environment first on `PATH`, while the service keeps running. | **Not relevant** | No console shell, `$` commands or terminal. |
| 183 | Changed | Custom start commands run without a shell and resolve their program from `.venv` and `node_modules/.bin` before `PATH`. | **Built in** | Commands run as argument lists without a shell; the program comes from the runtime table. Test: `test_10_every_compiled_argv_starts_with_an_absolute_program`. |
| 184 | Changed | Selecting a service shows its log scrolled to the bottom. | **Built in** | Selecting a service shows the end of its log (`ui/console.py`). |
| 185 | Fixed | Windows: Full access Python services exited immediately with code 106. | **Built in** | The runtime table uses the venv's real python, never the Windows launcher stub. |
| 186 | Fixed | Plugins read Pythonator's messages as UTF-8 on every Windows code page ("surrogates not allowed" in the AI assistant). | **Built in** | Plugin pipes are UTF-8 on every OS (`plugins/host.py`). |
| 187 | Fixed | Health checks use the port a server actually listens on. | **Built in** | Health checks use the configured port (`model/service.Health`). |
| 188 | Fixed | Guard names its whole-computer rule correctly. | **Built in** | Guard's rules have clear names (`resources/plugins/guard`). |

## 0.0.7

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 189 | Fixed | Windows: starting, stopping or restarting a service showed the busy mouse pointer. | **Built in** | Every subprocess on Windows uses CREATE_NO_WINDOW. |

## 0.0.6

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 190 | Added | **Settings**: start services one at a time (default), a few at a time, or all at once; log rotation size and count; keep running in the tray when the window is closed; start at sign-in, in the tray. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 191 | Added | Service list: multiple selection with Ctrl or Shift, reordering with ↑ ↓ or Alt+↑ / Alt+↓, and starting, stopping or removing several services from the context menu. | **Not relevant** | Not in 0.0.1 (not in the wireframes). The command palette selects services. |
| 192 | Removed | The separate start-order window. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 193 | Fixed | Two services in the same folder created its `.venv` at the same time. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |

## 0.0.5

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 194 | Added | Start queue for **Start all** and sign-in: list order, 4 at a time by default (**Settings → Start at once**). | **Not relevant** | Not in 0.0.1 (not in the wireframes). The reconciler starts services as soon as they are approved. |
| 195 | Added | **Settings**: start order and removing several services. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 196 | Changed | A first start skips setting up pip when the project has nothing to install. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |

## 0.0.4

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 197 | Changed | The console spreads output over every frame (60 per second, 30 under a flood); the "skipped" note appears at most once a second. | **Built in** | The console is virtual: only visible rows are drawn, whatever the flood. |
| 198 | Changed | Output is read in chunks instead of line by line (from 84% to 7% of a core in a flood test), and the console caps what a single frame draws. | **Built in** | Output is read by asyncio readers in the daemon and written to the log store. |

## 0.0.3

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 199 | Fixed | Sandbox mode: set-up steps could not write the project's `.venv`, which was also bound read-only. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |

## 0.0.2

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 200 | Added | **Own environment** per service, on by default: Start creates `.venv`, `node_modules` or `vendor` when missing; Deno, Go, .NET and Rust get a private package cache. | **Not relevant** | Pythonator runs no package installs in 0.0.1. Each service uses its project's own environment. |
| 201 | Added | **Start all** and sign-in start services one after another. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 202 | Fixed | The editor stalled while a service flooded its console. | **Built in** | The console is virtual, so a flood never stalls the editor. |
| 203 | Fixed | Plugin panel redraws took keyboard focus from the editor. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |

## 0.0.1

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 204 | Added | Services with restarts and back-off, schedules (intervals or cron), memory and CPU limits for the whole process tree, live CPU, memory and ports, and rotating logs with secrets masked. | **Built in** | Restarts, back-off, cron schedules, kernel limits, live stats (`reconcile/reconciler.py`). |
| 205 | Added | Sandbox mode by default (Landlock, seccomp and bubblewrap on Linux; Seatbelt on macOS; AppContainer and a job object on Windows), Container mode with hardened Docker or Podman, and Full access after confirmation. | **Built in** | Sandbox, Container, WSL and Full access per OS (`policy/compile.py`). |
| 206 | Added | Per-service ports, local only or network-wide, and **Make reachable** on Windows. | **Built in** | Ports per service with this-computer or LAN reach; Windows Sandbox refuses ports instead of Make reachable. |
| 207 | Added | Python, Node.js, Bun, Deno, Go, Rust, Java, .NET, Ruby, PHP, PowerShell, shell scripts or any command. | **Built in** | Python, Node, Bun, Deno, Go, Rust, Java, .NET, Ruby, PowerShell and any program (`policy/runtimes.py`). |
| 208 | Added | Sandboxed plugins: Webhooks, Guard, Health checks and the AI assistant. | **Built in** | Isolated plugins: Guard, Notify, AI assistant, Builder (`resources/plugins`). |
| 209 | Added | Built-in editor, Git clone and pull, and installers for Windows, macOS and Linux (x64 and arm64). | **Built in** | Built-in editor, private snapshots instead of pull, and one-command installers (`install.sh`, `install.ps1`). |

## Preview 0.0.11

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 210 | Added | Per-service **CPU limit** in percent of one core for the whole process tree: a hard cap on the job object on Windows, `--cpus` for containers, and on Linux and macOS the tree is paused in short slices while over the l… | **Built in** | CPU limits as cgroup CPUQuota, job CPU rate or `--cpus`; no SIGSTOP slicing. |
| 211 | Added | The console lists a service's limits and how they are enforced at each start; the header shows CPU use against the limit. | **Built in** | The plan lists each limit and how it is enforced. |
| 212 | Added | Guard: when the computer stays above 95% CPU or 92% memory for 30 s, the service using the most receives Guard's action, if it uses at least a quarter of all cores or a tenth of memory. | **Not relevant** | Guard reviews changes; no whole-computer load action in 0.0.1. |
| 213 | Added | AI assistant, rebuilt as a sandboxed official plugin (off by default): Claude, OpenAI, Gemini, DeepSeek, Ollama or any OpenAI-compatible server. | **Built in** | Isolated official AI plugin, off by default; edits are reviewed before apply. |
| 214 | Added | Plugin API 2: text box, drop-down and code block panels; `read_files` (read-only, secrets hidden) and `propose_changes` permissions. | **Built in** | `read_files` (secrets hidden) and exact-text proposals; no shell or script changes apply without review. |
| 215 | Added | `inspect` plugins receive the computer's CPU and memory (`self.system`) and each service's `cpu_limit`. | **Not relevant** | Plugin panels are gone: plugins show data rows and actions inside Pythonator's own pages. |
| 216 | Added | **Help ▾ → About Pythonator** with credits, support link and versions; **Help ▾** opens the documentation. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 217 | Changed | Plugin `secret` settings are stored in the system keychain (Windows Credential Manager, macOS Keychain, Secret Service) rather than `settings.json`; existing secrets move on their next save. | **Built in** | Plugin and service secrets live in the OS keychain (`reconcile/secrets.py`). |

## Preview 0.0.10

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 218 | Added | A background service runs services independently of the window; a crash of either leaves services running, and leftovers are cleaned up. | **Built in** | pythonatord runs services; the window is a client; leftovers are cleaned from the ledger. |
| 219 | Added | Restart back-off from 1 s to 60 s with a limit per 10 minutes; schedules with intervals, `@daily` or cron; a memory limit for the whole process tree. | **Built in** | Back-off and a give-up limit, cron schedules, tree memory limits. |
| 220 | Added | Package installs inside the service's isolation (`.venv`, `node_modules`, `vendor`). | **Not relevant** | Pythonator runs no package installs in 0.0.1. |
| 221 | Added | Environment check before each start; **System check** for the sandbox protections enforced. | **Built in** | Validation before each start; System check per machine. |
| 222 | Added | Isolation modes **Sandbox** (default), **Container** and **Full access**; a service does not start if a protection cannot be applied. | **Built in** | A layer that can't be applied refuses the start. Test: `test_8_missing_layer_refuses`. |
| 223 | Added | Ports per service, local only or other computers too. | **Built in** | Ports per service; No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 224 | Added | Containers with all capabilities dropped, `no-new-privileges`, a read-only image, a process limit, and optional container networks. | **Built in** | All caps dropped, no-new-privileges, read-only image, pids limit, own network per service. |
| 225 | Added | Plugins in sandboxed processes with a validated protocol; official plugins verified by SHA-256 against a reviewed catalogue; third-party plugins start in test mode with no permissions and are disabled on floods, inval… | **Built in** | Isolated plugins, strict protocol, official by pinned hash. Test: `test_official_plugin_hashes_are_pinned_by_the_release`. |
| 226 | Added | QScintilla editor; hardened Git clone and fast-forward-only pull; documented API with `docs/examples/pyctl.py`. | **Built in** | QScintilla editor; No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 227 | Added | Installers: Windows `setup.exe` and `.zip`, macOS `.dmg` (Apple silicon and Intel), Linux `.deb` (x64, arm64) and `.tar.gz`. | **Built in** | Installers are `install.sh` and `install.ps1` from the zip; the daemon is stopped before an update. |
| 228 | Added | CI on Windows, macOS and Linux with installer tests; `pip-audit` on hash-pinned lock files; attested releases. | **Built in** | CI on Linux, macOS and Windows; pip-audit on the hash-locked lock; attested releases (`.github/workflows/`). |
| 229 | Changed | The console draws for at most 10 ms per frame and skips what does not fit (with a note; the log keeps everything); only the displayed service's output reaches the window; fast writers block on their pipe instead of bu… | **Built in** | The console is virtual: it draws visible rows only; the log keeps everything. |
| 230 | Changed | One editor zoom level for all files (Ctrl+, Ctrl-, Ctrl+=, Ctrl+wheel, Ctrl+0 resets), remembered. | **Not relevant** | Not in 0.0.1 (not in the wireframes). |
| 231 | Changed | Start all and Stop all return immediately; a second Stop kills whatever is still stopping. | **Built in** | Start and Stop return at once; the reconciler does the work. |
| 232 | Removed | The AI assistant plugin, plugin `pyproject.toml` packaging, `bot.json` and other old-format migrations, the in-app API reference generator, and the code signing scripts. | **Not relevant** | Removed in the old app too. |

## Preview 0.0.9

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 233 | Added | Windows sandbox: **Make reachable…** adds a firewall rule admitting this PC only, so a sandboxed server answers on `http://127.0.0.1:<port>/`. | **Not relevant** | No Make reachable: Windows Sandbox refuses ports (NET-WIN-002), so no firewall rule, loopback exemption or permit is ever made. |
| 234 | Added | Dependencies menu for Python: **Create venv**, **Recreate venv…** and a **Use .venv** switch; other runtimes show where their packages go. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |
| 235 | Changed | A created venv is used from then on, even if the service was set to another Python. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |

## Preview 0.0.8

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 236 | Added | Python services ask before running outside the project's venv (none, unused or broken), with a remembered choice per service. | **Not relevant** | Pythonator runs no package installs in 0.0.1. The runtime table picks the project's `.venv` Python. |
| 237 | Added | `$` commands that install account-wide (`npm -g`, `yarn global`, `gem install`, `composer global`, `pip --user`) ask first and offer the project-local command. | **Not relevant** | No console shell, `$` commands or terminal. |
| 238 | Changed | After creating or recreating the venv, the requested start, install or command runs in it. | **Not relevant** | Pythonator runs no package installs in 0.0.1. |

## Preview 0.0.7

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 239 | Fixed | Windows sandbox: "working folder read only" was not enforced for services previously set to "read & write"; writes allowed by another folder's permissions are now blocked too. | **Built in** | Read-only grants add a deny entry when an inherited entry could write. Test: `tests/test_winjail.py::test_read_only_denies_write_inherited_from_above`. |

## Preview 0.0.6

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 240 | Removed | The command line added in 0.0.5. | **Not relevant** | The old command line; `pythonatorctl` is the CLI now. |

## Preview 0.0.5

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 241 | Added | Dependency vulnerability checks on every change and weekly; Dependabot for the app's and official plugins' requirements. | **Built in** | pip-audit on every change and weekly; Dependabot with a cooldown. |
| 242 | Fixed | Windows sandbox: temporary files go to the sandbox's own temporary folder instead of the service's folder. | **Built in** | The sandbox's `sitecustomize` sends temp files to its own folder (`jail/winjail.py`). |

## Preview 0.0.4

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 243 | Fixed | Windows sandbox: first attempt at keeping temporary files out of the service's folder (completed in 0.0.5). | **Built in** | Same, completed. |

## Preview 0.0.3

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 244 | Added | Guard plugin rewritten as a security policy: 19 rules for isolation, exposed ports, outgoing connections, container images and a resource watchdog; each can warn, stop or restart, with per-service exceptions. | **Built in** | Guard reviews every change with rules for mode, ports, LAN, exposure and memory (`resources/plugins/guard`). |
| 245 | Added | Webhooks plugin: Discord, Telegram, WhatsApp, Slack, Teams, ntfy, Pushover, Gotify or a custom endpoint, batched per service and rate-limited. | **Built in** | Notify sends to Slack, Discord, ntfy or any webhook, escaped and rate-limited. |
| 246 | Added | Plugin SDK 2: service set-up and resource use; multi-line and tabbed settings. | **Built in** | The plugin SDK (`resources/sdk`). |
| 247 | Added | Outgoing connections are recorded per service alongside its listening ports. | **Built in** | The gate records outgoing connections per service (`net/gate.py`). |
| 248 | Fixed | Windows sandbox: pip installs and Python's `tempfile` work. | **Built in** | Python's `tempfile` works in the Windows sandbox (`winjail.SITECUSTOMIZE`). |

## Preview 0.0.2

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 249 | Fixed | Windows sandbox: **Create venv** works and the venv always has pip; **Install dependencies** adds pip to a venv without it; `$` commands run in cmd (PowerShell cannot run in the sandbox); services no longer inherit Py… | **Not relevant** | Pythonator runs no package installs in 0.0.1. No console shell, `$` commands or terminal. |

## Preview 0.0.1

| # | Section | Entry | Verdict | Where, why or test |
| --- | --- | --- | --- | --- |
| 250 | Added | Services in Python, Node.js, Bun, Deno, Go, Java, .NET, Ruby, PHP, shell, PowerShell or any program. | **Built in** | Same runtimes, plus any program. |
| 251 | Added | Restarts with back-off and a crash-loop limit, schedules, memory and CPU limits, autostart and start at sign-in. | **Built in** | Restarts, back-off, give-up, schedules, limits; start at sign-in through `pythonatorctl service install`. |
| 252 | Added | A background service keeps services running while the window is closed. | **Built in** | pythonatord keeps services running with no window. |
| 253 | Added | A sandbox per service on Linux, macOS and Windows, and Docker / Podman containers. | **Built in** | A sandbox per service on each OS, and Podman / Docker containers. |
| 254 | Added | An editor with highlighting and completion, a console with colours, rotating logs and history. | **Built in** | Editor with highlighting and ruff problems; virtual console; segmented logs. |
| 255 | Added | Git clone and pull before each start. | **Not relevant** | No git pull or clone in 0.0.1: snapshots are local commits into a private git dir in the data folder (`work/git.py`). |
| 256 | Added | Plugins: official ones verified against a reviewed catalogue; third-party ones always sandboxed. | **Built in** | Official plugins by pinned hash; third-party plugins always isolated. |
| 257 | Added | A website with guides, a glossary, a dependency list and an API reference. | **Not relevant** | No website in this zip; docs/ is the documentation. |
