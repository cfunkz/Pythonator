# Pythonator documentation

Pythonator keeps long-running programs running: Discord and Telegram bots, web APIs, workers, scrapers and
scheduled jobs. Each one is a **service**. Pythonator starts it, restarts it when it fails, shows its output, keeps
it to the files and ports it needs, and keeps it running after the window is closed. It has a code editor built in,
and **plugins** add alerts, security rules, health checks and whatever else you write.

It runs on Windows, macOS and Linux, and runs Python, Node.js, Bun, Deno, Go, Java, .NET, Ruby, PHP, Rust, shell
scripts, PowerShell, and any other command.

## Using Pythonator

| Page | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Install, your first service, where your data is kept, keyboard shortcuts |
| [Services](services.md) | Restarts, schedules, memory and CPU limits, environment variables, the console and logs |
| [Isolation modes](isolation.md) | Sandbox, Container and Full access: what each one allows, and why Sandbox is the default |
| [Ports and APIs](networking.md) | Making a server reachable from this computer or your network, and connecting to containers |
| [Languages](languages.md) | How each language is found, started and given its packages |
| [Git](git.md) | Cloning a repository as a service and pulling new code |
| [Plugins](plugins.md) | Official and third-party plugins, permissions and test mode |
| [FAQ](faq.md) | Answers to common questions and problems |

## Building on it

| Page | What it covers |
| --- | --- |
| [Writing a plugin](plugin-development.md) | A plugin in 20 lines, then a policy plugin, a watchdog and a panel |
| [Plugin API](plugin-api.md) | `plugin.toml`, the Python SDK, permissions, panels and the message protocol |
| [Background service API](service-api.md) | The local socket the window uses, for your own scripts and tools |
| [Adding a language](adding-a-language.md) | For forks: one table entry adds a runtime, its installer and its container image |
| [Architecture](architecture.md) | The processes, threads and files, and why each part is where it is |

## Trust and the project

| Page | What it covers |
| --- | --- |
| [Security](security.md) | What Pythonator protects against, how each system enforces it, and what it doesn't do |
| [Glossary](glossary.md) | The technical words used in these pages |
| [Development and CI](development.md) | Tests, checks, the official plugin catalog, releases and this website |
| [Changelog](../CHANGELOG.md) | What changed in each version |

In the app, **Help ▾ → About Pythonator** shows the credits and versions, **Help ▾ → Support** opens these pages, questions, bug reports and private security reports, and **Help ▾ → Licensing**
shows the licence and the components Pythonator is built on.
