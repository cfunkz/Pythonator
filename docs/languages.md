# Languages

Pythonator runs a service with the language's own tools, found on your `PATH`. It never looks in the project folder
for them, so a project can't put its own `python` or `node` in front of the real one.

| Language | Found as | A file runs as | With no file | Install packages | Packages go to | Container image |
| --- | --- | --- | --- | --- | --- | --- |
| Python | `python3`, `python` (Windows: `python`, `py`) | `python -u bot.py` | | venv, then pip | `.venv` in the project | `python:3.13-slim` |
| Node.js | `node` | `node server.js` | `npm start` | `npm ci` / `npm install` / `pnpm` / `yarn` | `node_modules` | `node:22-slim` |
| Bun | `bun` | `bun run index.ts` | | `bun install` | `node_modules` | `oven/bun:1` |
| Deno | `deno` | `deno run --allow-all main.ts` | | | Deno's cache, private to the sandbox | `denoland/deno:2` |
| Go | `go` | `go run main.go` | `go run .` | `go mod download` | the Go module cache | `golang:1.24` |
| Java | `java` | `java -jar app.jar`, `java Main.java` | | | your build's own folders | `eclipse-temurin:21` |
| .NET | `dotnet` | `dotnet run --project app.csproj` | `dotnet run` | `dotnet restore` | the NuGet cache | `mcr.microsoft.com/dotnet/sdk:9.0` |
| Ruby | `ruby` | `ruby main.rb` | | `bundle install` | `vendor/bundle` | `ruby:3.4-slim` |
| PHP | `php` | `php index.php` | | `composer install` | `vendor` | `php:8.4-cli` |
| Rust | `cargo` | | `cargo run --release` | `cargo fetch` | `target` and the Cargo cache | `rust:1-slim` |
| Shell | `bash`, `sh` | `bash run.sh` | | | | `debian:stable-slim` |
| PowerShell | `pwsh`, `powershell` | `pwsh -NoProfile -File run.ps1` | | | | `mcr.microsoft.com/powershell` |
| Custom command | the first word | exactly what you type | | | | |

Arguments go after the file. A `.ts` file picks Bun when Bun is installed, Deno when only Deno is; you can change it in the Language list.

## Python and virtual environments

Pythonator uses the project's own virtual environment when it has one, in `.venv`, `venv` or `env`: the service
runs with that environment's Python, and its packages are there. **Install packages** creates `.venv` when there is
none, and remakes it when it is broken (for example when the Python it was made from was uninstalled).

The environment check before each start warns about a project with a `requirements.txt` or `pyproject.toml` but no
virtual environment.

A Python service in a sandbox can read its `.venv` and the Python installation it was made from, and nothing else of
yours.

## Deno

Deno has its own permission prompts. A background service can't answer them, so Pythonator runs Deno with
`--allow-all` and lets its own sandbox do the limiting. Pass Deno's flags yourself under **Arguments** (or as a
custom command) if you want both.

## A language that isn't in the list

Use **Custom command**: the program and its arguments, as you would type them, for example
`./target/release/mybot --config prod.toml` or `elixir --no-halt app.exs`. A sandboxed custom command can read the
folder its program is installed in.

To add a language to the list for everyone, or in your fork, see [Adding a language](adding-a-language.md). It is one
entry in a table.
