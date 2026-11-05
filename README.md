<p align="center">
  <img src="website/assets/icon-256.png" alt="" width="96" height="96">
</p>

<h1 align="center">Pythonator</h1>

<p align="center">
  Run your scripts, web servers and bots on your own computer: sandboxed, supervised, and kept running.<br>
  <a href="https://cfunkz.github.io/Pythonator/">Website</a> ·
  <a href="https://cfunkz.github.io/Pythonator/docs/">Documentation</a> ·
  <a href="https://cfunkz.github.io/Pythonator/docs/faq.html">FAQ</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="screenshots/main.png" alt="Pythonator: a sandboxed web API running, with its files, code, console and the Guard plugin panel" width="900">
</p>

Pythonator is a desktop app for the programs you leave running: a Discord bot, a FastAPI or Express server, a
scraper on a schedule. Add a folder, press Start, and it keeps the program running, restarts it when it crashes,
and keeps it in a sandbox so it can't read your files or reach ports you didn't give it.

* **Supervised.** Restarts with back-off, schedules (intervals or cron), memory and CPU limits for the whole process tree,
  live CPU, memory and ports, rotating logs with secrets masked. Services keep running when the window closes.
* **Isolated.** Sandbox mode (default) uses the operating system's own: Landlock, seccomp and bubblewrap on Linux,
  Seatbelt on macOS, AppContainer and a job object on Windows. Container mode runs it in hardened Docker or Podman.
  Full access asks first and shows in red.
* **Ports and APIs.** List the ports a server may use, and choose whether only this computer or your network too may
  connect. On Windows, **Make reachable** sets up the loopback exemption, a loopback permit and firewall rules for that service alone, so
  the browser on the same PC can open it.
* **Plugin-first.** Plugins run in their own sandboxed process. Official ones are checked by hash; third-party ones
  show a warning and start in test mode. Webhooks, Guard and Health checks are included.
* **Many languages.** Python (with its `.venv`), Node.js, Bun, Deno, Go, Rust, Java, .NET, Ruby, PHP, PowerShell and
  shell scripts, or any command. **Install packages** runs inside the same isolation.
* **A real editor.** QScintilla: highlighting for every language, folding, multiple carets, fast with big files.
* **Git.** Clone with hooks and submodules off, pull fast-forward only.

<table>
  <tr>
    <td><img src="screenshots/isolation.png" alt="The Isolation tab of a service" width="420"></td>
    <td><img src="screenshots/plugins.png" alt="The Plugins window, with Guard verified as official" width="420"></td>
  </tr>
  <tr>
    <td><img src="screenshots/editor.png" alt="The built-in editor" width="420"></td>
    <td><img src="screenshots/system-check.png" alt="System check: what the sandbox enforces on this computer" width="420"></td>
  </tr>
</table>

## Install

Download the installer for your system from [Releases](https://github.com/cfunkz/Pythonator/releases): a
`setup.exe` for Windows, a `.dmg` for macOS (Apple silicon and Intel), a `.deb` for Ubuntu, Debian and Raspberry Pi
OS, and portable `.zip` / `.tar.gz` files. Each release comes with a `SHA256SUMS.txt` and signed build provenance.
Or run it from source with Python 3.12 or later:

```bash
git clone https://github.com/cfunkz/Pythonator.git
cd Pythonator
python -m venv .venv
. .venv/bin/activate                       # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
pythonator
```

Then follow [Getting started](https://cfunkz.github.io/Pythonator/docs/getting-started.html).

## Documentation

| For | Read |
| --- | --- |
| Using it | [Getting started](docs/getting-started.md) · [Services](docs/services.md) · [Isolation modes](docs/isolation.md) · [Ports and APIs](docs/networking.md) · [Languages](docs/languages.md) · [Git](docs/git.md) · [Plugins](docs/plugins.md) |
| Building on it | [Writing a plugin](docs/plugin-development.md) · [Plugin API](docs/plugin-api.md) · [Background service API](docs/service-api.md) · [Adding a language](docs/adding-a-language.md) · [Architecture](docs/architecture.md) |
| Trust | [Security](docs/security.md) · [SECURITY.md](SECURITY.md) |
| Working on Pythonator | [Development and CI](docs/development.md) |

The same pages, with a menu and search-friendly navigation, are on the [website](https://cfunkz.github.io/Pythonator/docs/).

## Support

* Questions: [Discussions](https://github.com/cfunkz/Pythonator/discussions) ·
  Bugs and ideas: [Issues](https://github.com/cfunkz/Pythonator/issues) ·
  Contact: [hello@routezero.tech](mailto:hello@routezero.tech)
* Like it? [Buy me a coffee ☕](https://ko-fi.com/cfunkz81112) or star the repository.

Pythonator is made by [cfunkz](https://github.com/cfunkz). **Help ▾ → About Pythonator** in the app shows the
credits and the versions to paste into a bug report.

## Security

Please report security problems privately through
[GitHub's advisories](https://github.com/cfunkz/Pythonator/security/advisories/new), not in public issues. See
[SECURITY.md](SECURITY.md).

## License

[GPL-3.0](LICENSE). **Help ▾ → Licensing** in the app lists Pythonator's license and every library it uses.

---

<sub>Pythonator is designed and maintained by [cfunkz](https://github.com/cfunkz) as a solo project. AI-assisted
tooling (Claude Code) was used during development. Every release is built, installed and started in CI on Windows,
macOS and Linux before it is published.</sub>
