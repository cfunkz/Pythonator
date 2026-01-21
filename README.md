# Pythonator

**Pythonator** is a simple desktop app for running and managing multiple Python scripts side-by-side without living in terminal tabs. It’s built for developers who want fast iteration, clear logs, and simple process control.

Think of it as a local “script runner” with a GUI.

<p align="left">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13%2B-blue" />
  <img alt="PyQt6" src="https://img.shields.io/badge/UI-PyQt6-41CD52" />
  <img alt="License" src="https://img.shields.io/badge/License-MIT-black" />
</p>

---

## Screenshots
<p align="center">
  <img src="screenshots/image.png" alt="Pythonator - Main window" width="92%">
</p>

<p align="center">
  <img src="screenshots/logs.png" alt="Pythonator - Live logs & tabs" width="45%">
  <img src="screenshots/editor.png" alt="Pythonator - Built-in editor" width="45%">
</p>

<details>
  <summary><b>Venv Setup & History</summary>
  <br/>
  <p align="center">
    <img src="screenshots/history-search.png" alt="Pythonator - History + Search mode" width="92%">
  </p>
  <p align="center">
    <img src="screenshots/venv.png" alt="Pythonator - Venv + deps setup" width="92%">
  </p>
</details>

---

## What it does

* Run **multiple Python scripts** at the same time
* Each script gets its **own live log tab**
* Full **ANSI color support** (logs look like a real terminal)
* **Start / stop / restart** scripts individually or all at once
* Optional **auto-restart on crash**
* **Per-script virtual environments**
* Install dependencies from the UI
* Built-in **Python editor** (syntax highlighting + autocomplete)
* View **CPU and RAM usage** per running process
* Persistent logs saved to disk

---

## Why it exists

Pythonator was made to remove the friction of:

* switching terminals
* remembering which venv belongs to which script
* scrolling through noisy logs
* restarting crashed processes manually
* running long lived services

It’s especially useful for:

* local bots
* background workers
* small services
* experiments you want to keep running while you code

---

## Key features (quick list)

* **Workspace-based setup** — each script has its own config
* **Live / history / search** log modes
* **Safe process isolation** (no zombie processes)
* **Cross-platform** (Windows, macOS, Linux)
* Dark UI by default (no eye strain)

---

## Getting started

### Requirements

* Python 3.13+
* `PyQt6` for the UI
* `psutil` for CPU/RAM stats
* `jedi` for editor autocomplete

### How to run it

```bash
git clone https://github.com/cfunkz/Pythonator.git
cd Pythonator
python app.py
```

---

## Typical workflow

1. Create a new workspace
2. Point it at a Python script
3. (Optional) Use the built-in script editor
4. (Optional) Set a custom Python path
5. (Optional) Add a startup command or flags
6. Set up a venv
7. Install dependencies
8. Hit **Start**
9. Watch logs stream in live

---

## License

MIT.
Use it, fork it, break it, improve it.
