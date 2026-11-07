# Adding a language

This page is for changes to Pythonator itself, in a fork or a pull request. Adding a language takes one entry in a
table; the window, the sandbox, Container mode and the environment check all pick it up from there. The second half
of the page shows where other common changes belong.

A development setup is described in [Development and CI](development.md).

## The runtime table

Each language is a `Runtime` in
[`pythonator/core/runtimes.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/core/runtimes.py):

```python
@dataclass(frozen=True, slots=True)
class Runtime:
    key: str                      # stored in services.json: never change it once released
    label: str                    # shown in the Language list
    programs: tuple[str, ...]     # looked up on PATH, first found wins
    extensions: tuple[str, ...]   # entry files that mean this language
    markers: tuple[str, ...]      # files that mark a project of this language (for Detect)
    image: str                    # the default container image
    run: Callable[[str, str, Argv], Argv]          # (program, entry, args) -> the command
    install: Callable[[Path, str], list[Argv]] | None  # (folder, program) -> the install steps
    packages: str                 # where its packages go, shown in the window
```

## Example: Elixir

Add an entry to `RUNTIMES`:

```python
def _elixir_run(prog: str, entry: str, args: Argv) -> Argv:
    # A script runs with elixir; a Mix project (no entry) with mix run --no-halt.
    if entry:
        return [prog, entry, *args]
    return [which("mix") or "mix", "run", "--no-halt", *args]


RUNTIMES: dict[str, Runtime] = {r.key: r for r in (
    ...
    Runtime("elixir", "Elixir", ("elixir",), (".exs", ".ex"), ("mix.exs",), "elixir:1.18-slim",
            run=_elixir_run,
            install=lambda folder, prog: [[which("mix") or "mix", "deps.get"]],
            packages="deps and _build in the project"),
)}
```

That entry is enough for:

* the **Language** list to include Elixir, and **Detect from folder** to recognise `mix.exs`;
* a dropped `.exs` file to become an Elixir service;
* **Install packages** to run `mix deps.get` inside the service's isolation;
* the sandbox to grant read access to Elixir's installation folder (derived from the program's path);
* Container mode to use `elixir:1.18-slim`, with the project at `/app`;
* the environment check to report when `elixir` is not installed.

If the language has no default way to run a project without an entry file, add its key to the `needs_entry` check in
`service_argv` in [`pythonator/isolation/__init__.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/isolation/__init__.py).

### When the sandbox needs more

A sandboxed service can read the folder its program is installed in, and the system folders. Some languages need
more, such as a shared cache in the home folder or a second program. In the sandbox the home folder is a private one,
so a cache there works as normal and stays with the service. If the language runs a second program installed
elsewhere (as `npm` runs `node`), add it to `programs` in `program_folders`, in the same file, as Node.js does:

```python
if with_node and node_script and (node := runtimes.which("node")):
    programs.append(node)
```

Never grant the home folder or a credentials folder: `forbidden_read` and `forbidden_write`, also in
`isolation/__init__.py`, refuse them deliberately.

### Tests

Add the expected command to `test_run_commands` in
[`tests/test_core.py`](https://github.com/cfunkz/Pythonator/blob/main/tests/test_core.py), and a project to
`test_detect` if the language has a marker file. Then run the checks:

```bash
ruff check pythonator plugins tests && mypy && pytest
```

## Where other changes belong

Paths are relative to [`pythonator/`](https://github.com/cfunkz/Pythonator/tree/main/pythonator).

| Change | Where | Notes |
| --- | --- | --- |
| Run something before every start of a language | `run` in its `Runtime` | Return a longer argv; never a shell string |
| A new service field | `Service` in `core/models.py`, then the dialog in `ui/service_dialog.py` | Give it a default so that existing `services.json` files still load |
| A new check before starts | `service_report` in `core/envcheck.py` | Return `("ok" \| "warn" \| "error", text)` |
| A new background service request | A model in `daemon/protocol.py`, added to `Request`, and a `case` in `_dispatch` in `daemon/server.py` | Keep `extra="forbid"`; validate every field |
| A new plugin permission | `Permission` and `PERMISSIONS` in `plugins/manifest.py`, then the check in `plugins/host.py` | Describe it in terms a user understands |
| A new panel element | `Node` in `plugins/host.py`, a builder in `sdk.py`, and `render` in `ui/panels.py` | Text must remain plain text |
| A new isolation mode or platform | A module in `isolation/` with `wrap()` (and `capabilities()` for System check), called from `prepare()` | It must raise `IsolationError` rather than run with less protection |
| A new toolbar button | `_build_toolbar` in `ui/main_window.py` | The window only sends requests; the work belongs in the background service |
| A new colour | `Colors` in `ui/theme.py` | Both themes use the same names |

Most features need no change to Pythonator at all and are better written as a [plugin](plugin-development.md), which
can be shared without a fork and runs sandboxed.

## Rules that keep a fork safe

* Programs are started from argument lists, never through a shell.
* Everything from a user, a file or a plugin passes through `core/validate.py` or a
  [pydantic](https://docs.pydantic.dev/) model before use.
* The window never does the work; the background service does, and it never blocks on a plugin.
* If isolation cannot be applied, the service does not start.
* The sandbox launcher, [`launcher.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/launcher.py),
  imports only the standard library: it runs inside the sandbox before the service, and must stay small enough to
  review in one sitting.
