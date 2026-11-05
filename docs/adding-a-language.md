# Adding a language

This page is for people changing Pythonator itself, in a fork or a pull request. Adding a language takes one entry
in a table; the window, the sandbox, the container mode and the environment check pick it up from there. The rest of
the page shows where the other common changes go.

Set up a development copy first: see [Development and CI](development.md).

## The runtime table

Every language is a `Runtime` in [`pythonator/core/runtimes.py`](../pythonator/core/runtimes.py):

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

That's all it takes for:

* **Language** to list Elixir and **Detect from folder** to recognise a `mix.exs`;
* a dropped `.exs` file to become an Elixir service;
* **Install packages** to run `mix deps.get`, inside the service's isolation;
* the sandbox to let the service read Elixir's installation folder (found from the program's path);
* Container mode to use `elixir:1.18-slim` with the project at `/app`;
* the environment check to say when `elixir` isn't installed.

If the language has no default way to run a project without an entry file, add its key to the `needs_entry` check
in `service_argv` in [`pythonator/isolation/__init__.py`](../pythonator/isolation/__init__.py).

### When the sandbox needs more

A sandboxed service can read the folder its program is installed in, and the system folders. Some languages read
more: a shared cache in the home folder, or a second program. In the sandbox the home folder is a private one, so a
cache there just works and stays with the service. If the language runs a second program that lives elsewhere (as
`npm` runs `node`), add it to `programs` in `policy_for`, as Node.js does:

```python
if service.runtime in ("node", "bun") and (node := runtimes.which("node")):
    programs.append(node)
```

Never grant the home folder or a secrets folder: `forbidden_read` and `forbidden_write` in
[`pythonator/isolation/base.py`](../pythonator/isolation/base.py) refuse them, on purpose.

### Tests

Add the command to `test_run_commands` in [`tests/test_core.py`](../tests/test_core.py), and a project to
`test_detect` if it has a marker file. Run the checks:

```bash
ruff check pythonator plugins tests && mypy && pytest
```

## Other changes, and where they go

| You want to | Change | Notes |
| --- | --- | --- |
| Run something before every start of a language | `run` of its `Runtime` | Return a longer argv; never a shell string |
| A new field on services | `Service` in [`core/models.py`](../pythonator/core/models.py), then the dialog in [`ui/service_dialog.py`](../pythonator/ui/service_dialog.py) | Give it a default, so old `services.json` files still load |
| A new check before starts | `service_report` in [`core/envcheck.py`](../pythonator/core/envcheck.py) | Return `("ok" \| "warn" \| "error", text)` |
| A new request for the background service | A model in [`daemon/protocol.py`](../pythonator/daemon/protocol.py), added to `Request`, and a `case` in `_dispatch` in [`daemon/server.py`](../pythonator/daemon/server.py) | `extra="forbid"` stays on; validate every field |
| A new plugin permission | `Permission` and `PERMISSIONS` in [`plugins/manifest.py`](../pythonator/plugins/manifest.py), then the check in [`plugins/host.py`](../pythonator/plugins/host.py) | Describe it so a user understands it |
| A new panel element | `Node` in `plugins/host.py`, a builder in [`sdk.py`](../pythonator/sdk.py), and `render` in [`ui/panels.py`](../pythonator/ui/panels.py) | Text stays plain text |
| A new isolation mode or system | A module in [`isolation/`](../pythonator/isolation/) with `wrap()` and `capabilities()`, called from `prepare()` | It must raise `IsolationError` rather than run with less |
| A new toolbar button | `_build_toolbar` in [`ui/main_window.py`](../pythonator/ui/main_window.py) | The window only sends requests; the work goes in the background service |
| A new colour theme | `Colors` in [`ui/theme.py`](../pythonator/ui/theme.py) | Both themes use the same names |

Most features don't need a change to Pythonator at all: write a [plugin](plugin-development.md) instead. It can be
shared without a fork, and it runs contained.

## Rules that keep a fork safe

* Programs start from argument lists, never through a shell.
* Everything from a user, a file or a plugin goes through [`core/validate.py`](../pythonator/core/validate.py) or a
  pydantic model before it is used.
* The window never does the work; the background service does, and it never blocks on a plugin.
* If isolation can't be applied, the service doesn't start.
* The sandbox launcher ([`launcher.py`](../pythonator/launcher.py)) imports nothing but the standard library: it
  runs inside the sandbox, before the service, and must stay small enough to read in one sitting.
