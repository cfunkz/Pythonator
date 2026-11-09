# Development

Pythonator is built to be maintained by one person: under 20,000 lines of Python, one language, a small set of
dependencies, and tests that pin behaviour rather than implementation.

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Checks

```bash
ruff check pythonator tests
python -m pytest -q -p no:cacheprovider
```

Both must pass; CI runs them on Linux (with bubblewrap, and again without it), macOS and Windows. Tests that need
bubblewrap, git or a real AppContainer skip where those are missing, except in the CI job meant for them. `mypy
pythonator` is advisory (PyQt's stubs are noisy).

| Test file | Covers |
| --- | --- |
| `test_policy.py` | the compiler: golden argv per case, refusals, folder rules, the risk hash and widenings |
| `test_safe.py` | safe file reading (links, `..`, FIFOs, size caps) and secret masking |
| `test_store.py` | log range reads and masking on disk, the ledger surviving a torn line, spec generations and approvals |
| `test_schedule.py` | next run times and catch-up |
| `test_plugins.py` | manifests, content hashes, the bundled Guard |
| `test_service.py` | end to end: authentication (Unix socket and TCP), a sandboxed run, a hostile plugin trying to escape its jail, grants reset on new code, Guard settings, WSL verbs |
| `test_invariants.py` | the 16 invariants: one test each that fails CI (safe reads only, no environment crossing, snapshots never use the folder's git, Stop ends every child, missing layers refuse, hostile scripts per mode with a Full access control, absolute programs only, masking on every channel, docs match the compiler, a slot exception keeps the app alive, widenings change the hash, ledger recovery and reused pids) |
| `test_hardening.py` | one class-level test per finding closed while answering the security cases |
| `test_winjail.py` | Windows ACL grants and the AppContainer probe (runs for real on Windows) |

## Golden files

`tests/golden/*.json` hold the exact argv the compiler produces for each case. Any change to a jail shows up as a
diff in review. To accept an intended change:

```bash
UPDATE_GOLDEN=1 pytest tests/test_policy.py
git diff tests/golden
```

## Running from the source folder

```bash
PYTHONATOR_DATA=/tmp/pdata python -m pythonator            # the window, with a throwaway data folder
PYTHONATOR_DATA=/tmp/pdata python -m pythonator         # the window against it
```

## Generated files

| File | Regenerate with | Checked by |
| --- | --- | --- |
| `tests/golden/*.json` | `UPDATE_GOLDEN=1 pytest tests/test_policy.py` | `test_golden_argv` |
| `pythonator/resources/plugins/official.json` | `UPDATE_GOLDEN=1 pytest tests/test_hardening.py -k official` | `test_official_plugin_hashes_are_pinned_by_the_release` |
| the matrix in `docs/isolation.md` | `python -m pythonator.service.check --doc` (paste between the markers) | `test_12_docs_protection_tables_match_the_compiler` |
| `requirements.lock` | `uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.12 -o requirements.lock` | pip-audit in `security.yml` |
| `requirements-build.lock` | `pip download --no-deps setuptools` and its sha256 | the release build |

## Release checklist

1. `CHANGELOG.md` has a `## [x.y.z]` section, and `pythonator/__init__.py` and `version:` in `_config.yml` (the
   website's download links) say the same version.
2. CI and Security are green on the commit on `main`.
3. Check Qt's security advisories (pip-audit doesn't see them) for the PyQt6 version in the lock.
4. Run the manual Windows checklist on a real machine: a Sandbox service with Python and with Node.js, Stop, a
   folder grant taken back after Remove, uninstall from Settings → Apps.
5. Optionally run the release workflow by hand first (Actions → release → Run workflow): it builds and tests every
   download and publishes nothing.
6. Tag `vX.Y.Z` on `main` (or `vX.Y.Z-alpha`, `-beta.1` and so on for a prerelease, which is published as one;
   `version:` in `_config.yml` must be the tag without its `v`). The release workflow checks the tag, builds the wheel, then on each OS runs
   `packaging/build.py`: Windows `setup.exe` (Inno Setup, `packaging/pythonator.iss`) and `.zip`, macOS `.dmg` and
   `.zip` (Apple silicon and Intel), Linux `.deb` and `.tar.gz` (x64 and arm64), each with its own Python from uv.
   Each runner installs its download, runs it and uninstalls it. Then a reviewer approves the `release` environment,
   and the files are attested and published with `SHA256SUMS` and the CHANGELOG section as notes. A version is
   published once, never replaced.

To build this OS's downloads yourself: `pip wheel --no-deps --no-build-isolation -w dist .` then
`python packaging/build.py` (into `release/`, which is ignored by git).

Repository settings that go with it: immutable releases, a `v*` tag ruleset, `main` protected with the CI checks
required, and the `release` environment with a required reviewer.

## The website

The pages workflow (`.github/workflows/pages.yml`) builds <https://cfunkz.github.io/Pythonator/> with Jekyll on
every push to `main` (Settings → Pages → Source: GitHub Actions): the pages are the Markdown
files in `docs/`, the landing page and design are in `website/`, and the sidebar is `docs_nav` in `_config.yml`. To
add a page, create `docs/<name>.md` starting with a `# Title` and add it to `docs_nav`. Links between pages point at
the `.md` files, so they work on GitHub too. The version and download links on the landing page come
from `version:` in `_config.yml`.

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

`Gemfile`, `Gemfile.lock` and `vendor/` are excluded from the site and should not be committed.

## Rules for changes

* The compiler stays pure: no file or network access in `policy/compile.py`; facts come in through `HostFacts`.
* A new protection is a new `Layer` with an honest status. Never add a fallback that runs without it.
* A new widening must change `risk.projection()`, so it asks for approval again.
* A new plugin method must map to exactly one permission in `METHOD_PERMISSION`.
* The window never calls the OS to run anything; it asks `pythonatord`.
* Every read of a service folder goes through `safe/safefs.py`; every host tool gets `safe/hostenv.host_env()`.
* A finding gets a class-level test (any link, any special file, any variable), not one for the instance.
* Keep it under 20,000 lines.

## Notes

* mypy skips the PyQt6 stub files (`follow_imports_for_stubs`), because PyQt6 6.12 ships stubs that mypy can't
  parse.
* `QT_QPA_PLATFORM=offscreen` runs the window without a display, for screenshots in CI.
