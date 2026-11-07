# Languages

Pythonator runs each service with its language's own tools, found on `PATH`. It never looks for them in the project
folder, so a project cannot place its own `python` or `node` ahead of the real one.

| Language | Found as | A file runs as | With no file | Install packages | Packages go to | Container image |
| --- | --- | --- | --- | --- | --- | --- |
| Python | `python3`, `python` (Windows: `python`, `py`) | `python -u bot.py` | | venv, then pip | `.venv` in the project | `python:3.13-slim` |
| Node.js | `node` | `node server.js` | `npm start` | `npm ci` / `npm install` / `pnpm` / `yarn` | `node_modules` | `node:24-slim` |
| Bun | `bun` | `bun run index.ts` | | `bun install` | `node_modules` | `oven/bun:1` |
| Deno | `deno` | `deno run --allow-all main.ts` | | | Deno's cache, private to the sandbox | `denoland/deno:debian` |
| Go | `go` | `go run main.go` | `go run .` | `go mod download` | The Go module cache | `golang:1.27` |
| Java | `java` | `java -jar app.jar`, `java Main.java` | | | The build's own folders | `eclipse-temurin:21` |
| .NET | `dotnet` | `dotnet run --project app.csproj` | `dotnet run` | `dotnet restore` | The NuGet cache | `mcr.microsoft.com/dotnet/sdk:10.0` |
| Ruby | `ruby` | `ruby main.rb` | | `bundle install` | `vendor/bundle` | `ruby:3.4-slim` |
| PHP | `php` | `php index.php` | | `composer install` | `vendor` | `php:8.5-cli` |
| Rust | `cargo` | | `cargo run --release` | `cargo fetch` | `target` and the Cargo cache | `rust:1-slim` |
| Shell | `bash`, `sh` | `bash run.sh` | | | | `debian:stable-slim` |
| PowerShell | `pwsh`, `powershell` | `pwsh -NoProfile -File run.ps1` | | | | `mcr.microsoft.com/powershell` |
| Docker Compose | `docker`, `podman` (Container mode only) | `compose up` on `compose.yaml` | | | | From the Compose file |
| Custom command | The first word | Exactly as entered | | | | |

Arguments are placed after the file. A `.ts` file selects Bun when Bun is installed, or Deno when only Deno is; the
**Language** list can override this. The full table is `RUNTIMES` in
[`pythonator/core/runtimes.py`](https://github.com/cfunkz/Pythonator/blob/main/pythonator/core/runtimes.py).

Before each start, Pythonator compares the installed runtime with the version the project declares (`engines` in
`package.json`, `requires-python`, the `go` line in `go.mod`, `rust-version`, Composer's `php`) and notes a mismatch.

## Python and virtual environments

Pythonator uses the project's own [virtual environment](https://docs.python.org/3/library/venv.html) when it has one,
in `.venv`, `venv` or `env`: the service runs with that environment's Python and packages. **Install packages**
creates `.venv` when there is none, and recreates it when it is broken (for example, when its base Python has been
uninstalled). With **Own environment** ticked, a start does the same automatically.

The environment check before each start warns about a project with a `requirements.txt` or `pyproject.toml` but no
virtual environment. The venv's Python version is read from `pyvenv.cfg` rather than by running it, so nothing in the
project folder runs outside the sandbox.

A sandboxed Python service can read its `.venv` and the Python installation it was created from, and nothing else of
the user's.

## Deno

Deno has its own permission prompts, which a background process cannot answer. Pythonator therefore runs Deno with
`--allow-all` and relies on its own sandbox for confinement. To use both, pass Deno's
[permission flags](https://docs.deno.com/runtime/fundamentals/security/) under **Arguments**, or as a custom command.

## Languages not in the list

Use **Custom command** with the program and its arguments, for example `./target/release/mybot --config prod.toml` or
`elixir --no-halt app.exs`. A sandboxed custom command can read the folder its program is installed in.

To add a language to the list, in a fork or a pull request, see [Adding a language](adding-a-language.md). It takes
one entry in a table.
