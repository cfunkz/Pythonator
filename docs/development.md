# Development and CI

How to work on Pythonator itself: set it up, run the checks, change the official plugins, release, and preview this
website.

## Set up

```bash
git clone https://github.com/cfunkz/Pythonator.git
cd Pythonator
python -m venv .venv
. .venv/bin/activate                       # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
```

`requirements-dev.lock` holds the app's dependencies and the tools (ruff, mypy, pytest, pytest-qt, pip-audit), each
pinned with its hashes. Run the app with `pythonator`, and the background service alone with
`pythonator --daemon`. Set `PYTHONATOR_HOME=/tmp/pythonator-dev` to keep a development copy away from your real
services.

## Checks

```bash
ruff check pythonator plugins tests docs/examples
mypy                                         # strict, on pythonator/ and tests/
for p in plugins/*/plugin.py; do mypy "$p"; done
pytest -q -rs                                # -rs lists skipped tests and why
pip-audit --require-hashes --disable-pip -r requirements.lock
```

Everything must pass before a change is merged; CI runs the same commands.

The tests in [`tests/`](../tests/) include:

| File | Covers |
| --- | --- |
| `test_validate.py` | Every input check: names, variables, arguments, ports, git addresses, images |
| `test_core.py` | Models, the store and its recovery from bad files, the 0.0.9 import, runtimes, schedules, ANSI, log masking and rotation |
| `test_sandbox.py` | Escape attempts from inside the real sandbox: reading keys and the token, writing outside, other ports, the network, `ptrace` |
| `test_daemon.py` | The real background service over its socket: wrong tokens, bad requests, start and stop, input, crash loops, Full-access consent |
| `test_plugins.py` | The catalog, tampering, and hostile plugins: floods, huge and garbage messages, crash loops, forged control, reading secrets, hanging policy checks, bad panels |
| `test_official_plugins.py` | Guard's rules, Webhooks' payloads, Health's probe |
| `test_ui.py` | The window's parts offscreen, About, Licensing and Support included |

UI tests run offscreen (`QT_QPA_PLATFORM=offscreen`, set by `tests/conftest.py`). Sandbox tests skip, with the
reason, the protections a computer doesn't have.

## Rules for the code

* Type everything; `mypy --strict` must pass. Use pydantic models for anything read from a file, a socket or a plugin.
* Start programs from argument lists. No `shell=True`, no `os.system`.
* The window sends requests; the background service does the work. Neither blocks on a plugin or a pipe.
* Qt's JSON and CBOR classes are banned by the linter (they had CVE-2026-105924); use `json`.
* `launcher.py`, `sdk.py` and `plugins/worker.py` import only the standard library: they run inside sandboxes.
* Prefer a plugin to a new built-in feature.

## Official plugins

The official plugins are in [`plugins/`](../plugins/), and their SHA-256 hashes in
[`pythonator/plugins/official.json`](../pythonator/plugins/official.json). After changing any file of an official
plugin, regenerate the catalog and commit it with the change:

```bash
python -m pythonator.plugins.trust --update
```

`test_official_catalog_is_up_to_date` fails when they don't match, and in a running app a plugin that doesn't match
is treated as third-party. A reviewer of a pull request that changes `official.json` is approving the plugin code.

## CI

| Workflow | Runs | Does |
| --- | --- | --- |
| [`ci.yml`](../.github/workflows/ci.yml) | Every push and pull request | Lint and type checks; tests on Windows, macOS and Linux with Python 3.12 and 3.13 (Linux first with bubblewrap blocked, then with it); builds the app on each system and runs `packaging/smoke_test.py` |
| [`security.yml`](../.github/workflows/security.yml) | Every push and pull request, and weekly | `pip-audit` on the app, development and build lock files |
| [`release.yml`](../.github/workflows/release.yml) | A pushed `v*` tag | Builds, smoke tests, attests and publishes a release |
| [`dependabot.yml`](../.github/dependabot.yml) | Weekly | Update pull requests for actions and locked packages, after a 7-day cool-down |

Every action is pinned to a commit hash and every workflow starts with a read-only token. Turn on CodeQL's default
setup, Dependabot alerts and secret scanning in the repository's **Security** settings; they need no workflow.

## Dependencies

Direct dependencies are in [`pyproject.toml`](../pyproject.toml), with ranges. The lock files pin everything:

```bash
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile packaging/requirements-build.in --universal --python-version 3.12 --generate-hashes \
    -o packaging/requirements-build.lock
```

PyQt6 is held below 6.12.0 because its type stubs break mypy; lift the cap when a fixed version is out.

## Building the app

```bash
pip install --require-hashes -r packaging/requirements-build.lock
pip install --no-deps .
python packaging/build.py                                      # PyInstaller, then this system's packages
python packaging/smoke_test.py dist/Pythonator/Pythonator     # macOS: dist/Pythonator.app/Contents/MacOS/Pythonator
```

`build.py` puts this system's packages in `release/`: the Windows installer (with
[Inno Setup](https://jrsoftware.org/isdl.php) installed) and `.zip`, the macOS `.dmg`, or the Linux `.deb` (with
`dpkg-deb`) and `.tar.gz`. The smoke test starts the built app's background service, runs a sandboxed service
through it, and stops it with `shutdown`, the command the Windows installer runs before an update.
`packaging/test_installer.ps1` installs the Windows installer for this user, starts the app, and uninstalls it.

## Releasing

1. Set `__version__` in [`pythonator/__init__.py`](../pythonator/__init__.py) and add the version to
   [CHANGELOG.md](../CHANGELOG.md).
2. Commit, then tag and push: `git tag v0.0.1 && git push origin v0.0.1`.

`release.yml` checks the tag matches the version, builds the installers and portable files on Windows, Linux
(x64 and arm64) and macOS (Apple silicon and Intel), installs and starts each one, and publishes them with a
`SHA256SUMS.txt` and signed build provenance. CI builds and test-installs the same packages on every push.

## This website

GitHub Pages builds the site from the Markdown in [`docs/`](./), with the design in
[`website/`](../website/) and the menu in [`_config.yml`](../_config.yml). To add a page, write `docs/<name>.md`
starting with a `# Title`, and add it to `docs_nav` in `_config.yml`. Links between pages are written to the `.md`
files, so they work on GitHub too.

Preview it locally with Ruby:

```bash
cat > Gemfile <<'GEMS'
source "https://rubygems.org"
gem "github-pages", group: :jekyll_plugins
gem "webrick"
GEMS
bundle config set --local path vendor && bundle install
bundle exec jekyll serve --baseurl /Pythonator      # http://127.0.0.1:4000/Pythonator/
```

`Gemfile`, `Gemfile.lock` and `vendor/` are ignored by the site and by git.
