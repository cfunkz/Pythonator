# Development and CI

How to work on Pythonator itself: setting up, running the checks, changing the official plugins, building, releasing,
and previewing this website.

## Setting up

```bash
git clone https://github.com/cfunkz/Pythonator.git
cd Pythonator
python -m venv .venv
. .venv/bin/activate                       # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
```

`requirements-dev.lock` contains the app's dependencies and the development tools
([ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy.readthedocs.io/), pytest,
[pytest-qt](https://pytest-qt.readthedocs.io/), [pip-audit](https://github.com/pypa/pip-audit)), each pinned with its
hashes. `pythonator` runs the app and `pythonator --daemon` runs the background service alone. Setting
`PYTHONATOR_HOME=/tmp/pythonator-dev` keeps a development instance separate from real services.

## Checks

```bash
ruff check pythonator plugins tests docs/examples
mypy                                         # strict, on pythonator/ and tests/
for p in plugins/*/plugin.py; do mypy "$p"; done
pytest -q -rs                                # -rs lists skipped tests and why
pip-audit --require-hashes --disable-pip -r requirements.lock
```

All of these must pass before a change is merged; CI runs the same commands.

Selected test modules in [`tests/`](https://github.com/cfunkz/Pythonator/tree/main/tests):

| File | Covers |
| --- | --- |
| `test_validate.py` | Every input check: names, variables, arguments, ports, git addresses, images |
| `test_core.py` | Models, the store and its recovery from invalid files, the 0.0.9 import, runtimes, schedules, ANSI, log masking and rotation |
| `test_sandbox.py` | Escape attempts from inside the real sandbox: reading keys and the token, writing outside the project, other ports, the network, `ptrace` |
| `test_daemon.py` | The real background service over its socket: wrong tokens, invalid requests, start and stop, input, crash loops, Full access consent |
| `test_plugins.py` | The catalogue, tampering, and hostile plugins: floods, oversized and malformed messages, crash loops, forged control messages, reading secrets, hanging policy checks, invalid panels |
| `test_official_plugins.py` | Guard's rules, Notify's tracking, anomalies, webhook payloads and health probes, and the Project builder |
| `test_ai_plugin.py`, `test_ai_edits.py` | The AI assistant against a fake AI server, and parsing of edit blocks |
| `test_leftovers.py`, `test_builder_cleanup.py` | System check's *Left behind by services* and the builder's leftover folders |
| `test_e2e_node_sandbox.py` | A Vite app in the Windows sandbox, made reachable and loaded over HTTP (CI only, `PYTHONATOR_E2E=1`) |
| `test_ui.py`, `test_run_settings.py` | Parts of the window, offscreen, including About, Licensing, Support and the Limits and Schedule tabs |
| `test_git.py` | Git pulls: settings that run programs, links and files planted in `.git`, only the saved address, the timeout, pulls that wait for the services that may write the checkout |
| `test_isolation.py` | Sandbox rules without an escape attempt: refused folders, inherited secrets, devices, ports, process groups, files a service plants for Pythonator |
| `test_container.py` | Container mode: Compose projects and their check, the default images, folders it may not mount |
| `test_windows.py` | Node.js in the Windows sandbox, program folders, Make reachable, PowerShell and WSL |
| `test_runtimes.py`, `test_templates.py` | Runtime versions, ports and listeners; the Project builder and its templates |
| `test_editor.py` | The editor and the file tree |
| `test_repo.py` | The repository itself: no program by a bare name, current lock files and actions, no committed `.env` |

A fix adds its regression test to the file for its topic, not to a file per release.

UI tests run offscreen (`QT_QPA_PLATFORM=offscreen`, set in `tests/conftest.py`). Sandbox tests skip, with a reason,
any protection the computer lacks.

## Coding rules

* Type everything; `mypy --strict` must pass. Use pydantic models for anything read from a file, a socket or a plugin.
* Start programs from argument lists: no `shell=True`, no `os.system`.
* The window sends requests and the background service does the work. Neither blocks on a plugin or a pipe.
* Qt's JSON and CBOR classes are banned by a lint rule in `pyproject.toml`; use `json`.
* `launcher.py`, `sdk.py` and `plugins/worker.py` import only the standard library, as they run inside sandboxes.
* Prefer a plugin to a new built-in feature.

## Official plugins

The official plugins are in [`plugins/`](https://github.com/cfunkz/Pythonator/tree/main/plugins), and their SHA-256
hashes in [`pythonator/plugins/official.json`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/plugins/official.json).
After changing any file of an official plugin, regenerate the catalogue and commit it with the change:

```bash
python -m pythonator.plugins.trust --update
```

`test_official_catalog_is_up_to_date` fails when they differ, and at run time a plugin that does not match is treated
as third-party. Approving a pull request that changes `official.json` therefore means approving the plugin code.

## CI

| Workflow | Trigger | Steps |
| --- | --- | --- |
| [`ci.yml`](https://github.com/cfunkz/Pythonator/blob/main/.github/workflows/ci.yml) | Every push and pull request | Lint and type checks; tests on Windows, macOS and Ubuntu 24.04 with Python 3.12 and 3.13, and on Ubuntu 26.04 with 3.13 (on Linux, first with bubblewrap blocked by AppArmor, then with it allowed); a Node.js end-to-end test in the Windows sandbox; builds on each system, installs the package and runs `packaging/smoke_test.py` |
| [`security.yml`](https://github.com/cfunkz/Pythonator/blob/main/.github/workflows/security.yml) | Every push and pull request, and weekly | `pip-audit` on the app, development, build and lock-tool lock files |
| [`release.yml`](https://github.com/cfunkz/Pythonator/blob/main/.github/workflows/release.yml) | A pushed `v*` tag | Builds, smoke-tests, attests and publishes a release |
| [`refresh-locks.yml`](https://github.com/cfunkz/Pythonator/blob/main/.github/workflows/refresh-locks.yml) | Weekly, or by hand | Compiles every `*.lock` again with `--upgrade`, runs `pip-audit`, ruff, mypy and the tests, and opens or updates one pull request from the `refresh-locks` branch |
| [`dependabot.yml`](https://github.com/cfunkz/Pythonator/blob/main/.github/dependabot.yml) | Weekly | Update pull requests for actions and the version ranges in `pyproject.toml`, after a 7-day cooldown. It never reads the `*.lock` files |

Every action is pinned to a commit hash, and every workflow starts with a read-only token. CodeQL's default set-up,
Dependabot alerts and secret scanning are enabled in the repository's **Security** settings and need no workflow.

## Dependencies

Direct dependencies are declared in [`pyproject.toml`](https://github.com/cfunkz/Pythonator/blob/main/pyproject.toml)
with version ranges. The lock files, generated with [`uv pip compile`](https://docs.astral.sh/uv/pip/compile/), pin
everything:

```bash
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile packaging/requirements-build.in --universal --python-version 3.12 --generate-hashes \
    -o packaging/requirements-build.lock
uv pip compile packaging/requirements-locktool.in --universal --python-version 3.12 --generate-hashes \
    -o packaging/requirements-locktool.lock
```

The second line of each lock is the command that wrote it; `refresh-locks.yml` runs exactly that, with `--upgrade`.
The workflow needs **Settings > Actions > General > Workflow permissions > Allow GitHub Actions to create and approve
pull requests**. A pull request it opens doesn't start CI by itself (GitHub's rule for a workflow's own token):
close and reopen it to run CI on every system.

PyQt6 6.12.0 is excluded because its type stubs break mypy.

## Building the app

```bash
pip install --require-hashes -r packaging/requirements-build.lock
pip install --no-deps .
python packaging/build.py                                      # PyInstaller, then this system's packages
python packaging/smoke_test.py dist/Pythonator/Pythonator     # macOS: dist/Pythonator.app/Contents/MacOS/Pythonator
```

`build.py` runs [PyInstaller](https://pyinstaller.org/) and places this system's packages in `release/`: the Windows
installer (requires [Inno Setup](https://jrsoftware.org/isdl.php)) and `.zip`, the macOS `.dmg`, or the Linux `.deb`
(requires `dpkg-deb`) and `.tar.gz`. The smoke test starts the built app's background service, runs a sandboxed
service through it, and stops it with `shutdown`, the same command the Windows installer runs before an update.
`packaging/test_installer.ps1` installs the Windows build for the current user, starts it, and uninstalls it.

## Releasing

1. Set `__version__` in [`pythonator/__init__.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/__init__.py).
   In [CHANGELOG.md](../CHANGELOG.md), rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty
   `## [Unreleased]` above it, and update the compare links at the bottom
   ([Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)). The release notes are taken from that section.
   Update the version shown on the website (`website/index.html`).
2. Commit, then tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

`release.yml` checks that the tag matches `__version__`, builds the installers and portable archives on Windows,
Linux (x64 and arm64) and macOS (Apple silicon and Intel), installs and starts each one, and publishes them with
`SHA256SUMS.txt` and signed build provenance ([actions/attest](https://github.com/actions/attest)).
CI builds and test-installs the same packages on every push.

## The website

[GitHub Pages](https://docs.github.com/en/pages/setting-up-a-github-pages-site-with-jekyll) builds the site with
Jekyll from the Markdown in [`docs/`](./), with the design in
[`website/`](https://github.com/cfunkz/Pythonator/tree/main/website) and the navigation in
[`_config.yml`](https://github.com/cfunkz/Pythonator/blob/main/_config.yml). To add a page, create `docs/<name>.md`
starting with a `# Title`, and add it to `docs_nav` in `_config.yml`. Links between pages point at the `.md` files, so
they work on GitHub as well. Links to source files use full GitHub URLs, since the site does not publish the code.

To preview locally with Ruby:

```bash
cat > Gemfile <<'GEMS'
source "https://rubygems.org"
gem "github-pages", group: :jekyll_plugins
gem "webrick"
GEMS
bundle config set --local path vendor && bundle install
bundle exec jekyll serve --baseurl /Pythonator      # http://127.0.0.1:4000/Pythonator/
```

`Gemfile`, `Gemfile.lock` and `vendor/` are excluded from the site and ignored by git.
