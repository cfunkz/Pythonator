# Security model

Pythonator's first priority is that running a script for you never puts the rest of your computer at risk. This
page says what is defended, how, and what isn't. Report problems as described in
[SECURITY.md](../SECURITY.md).

## What it protects

1. **Your files** from the services you run: SSH keys, browser profiles, password stores, other projects.
2. **Your network and your computer's local programs** from services that shouldn't reach them.
3. **Your decisions**: nothing runs, and nothing gets more access, without your approval.
4. **Pythonator itself** from other local accounts, from the services it runs, and from its plugins.

## How

### Services can't leave their jail

On Linux, Sandbox mode builds every service a fresh bubblewrap jail and applies Landlock and a libseccomp filter
inside it, so access to files is limited twice: by what is mounted, and by what the kernel allows. `no_new_privs`
and the dropped capabilities stop setuid escalation; a new session stops terminal injection. The network is a
separate namespace with nothing in it unless you asked for traffic, and even then the host's `127.0.0.1` isn't
reachable. macOS uses a deny-by-default Seatbelt profile with every path passed as a parameter; Windows uses an
AppContainer in a job object, with folder grants recorded in a ledger and taken back when no longer needed. WSL
mode runs the Linux jail inside Pythonator's own distribution, with no drives and no interop. Details:
[Isolation](isolation.md).

### Refuse, never "cooperate"

If a layer the mode needs can't be built on this machine, the service doesn't start, and you get a code and a fix.
If the in-jail launcher can't apply Landlock, it exits with 126 before your program runs. A layer that is weaker on
this machine is labelled *reduced* in the plan, the security stack and the first console line.

### Approvals bound to a hash

Every service has a plan. You approve the plan, and the approval stores a SHA-256 of every field that could widen
access (mode, folders, traffic, hosts, ports, limits, secrets, image). On every start the hash is recomputed; if it
doesn't match, the service is **Blocked** until you look again. A tool, a plugin or a hand-edited spec file can't
widen a service silently.

### Folders that can never be granted

Your home, system folders, `~/.ssh` and other secret folders, login scripts and Pythonator's data folder are refused
in every mode, after resolving symlinks. See [Isolation](isolation.md#folders).

### The background service only talks to you

`pythonatord` listens on a Unix socket inside an owner-only folder (`0700`); on Windows, on loopback TCP with the
port in a private file. On Linux it checks the peer's user id (`SO_PEERCRED`). Then, on every OS, both sides prove they know a 256-bit key kept in `ipc.key` (`0600`) with HMAC-SHA256
over fresh nonces from both sides, so neither a different user nor an impostor can talk to the other, and the key never crosses the connection.
Messages are size-limited (4 MB) newline-delimited JSON.

### Owned, and only owned

Before Pythonator starts a process or container, it writes an *intend* entry to its ledger (fsynced), and *done* or
*undone* after. After a crash or a power cut, it finishes or undoes only what the ledger says it started. Containers
carry `pythonator.owner` labels. It never stops a process or container it didn't create.

### Secrets

Secrets stay in your system keyring. Specs hold names only. Values reach the service through its environment, never
a command line. Every line is masked before it is written: secret values, secret-looking names, URL passwords,
known token formats and PEM private keys. Anything that leaves through a plugin, the AI assistant or a webhook gets
a second mask over every configured value. Jails, plugins and host tools never inherit your environment: each gets
one built by Pythonator.

### Plugins can't do more than you granted

Each plugin runs in its own jail with no network, no home and its code read-only. It can only *ask* Pythonator for
things, and each request is checked against your grants on every call. Grants are tied to the hash of the plugin's
code, so an update can't inherit trust. A plugin trusted to review changes fails closed. Safe mode stops them all.
Details: [Plugins](plugins.md).

### Dependencies

Few runtime dependencies, all widely used, with major versions capped: PyQt6 (and QScintilla), pydantic, psutil,
cronsim, platformdirs, keyring, plus uv and ruff as tools. Every one is installed from `requirements.lock` with
`--require-hashes`. The jail launcher and the plugin SDK use only the standard library, so nothing from PyPI runs
inside a jail unless your own project brings it. Qt's JSON and CBOR parsers are banned in the code.

## What isn't defended

* **Full access** is what it says. Use Guard to restrict where it is allowed. A Full access service can't share a
  folder with an isolated one (`RUN-FOLDER-001`).
* **The same user account.** A program you run yourself, outside Pythonator, as the same user, can read Pythonator's
  data folder. Pythonator defends against its services and plugins, not against you.
* **Weaker layers on some machines** are labelled *reduced* and listed per OS in [Isolation](isolation.md); the
  remaining gaps are under [Known limitations](isolation.md#known-limitations), and every open case from earlier
  audits is at the top of [Security cases](security-cases.md).
