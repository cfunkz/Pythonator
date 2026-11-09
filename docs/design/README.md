# Design

These twelve screens are the approved design for Pythonator, and the 0.0.1 window is built from all of them. Numbers and versions inside the
mock-ups are sample data; in the app, every number is measured on your machine.

## The look

* **Cobra**, from the logo: deep navy (`#06172a`, `#081c31`, `#0b2540`) with one solid chartreuse accent (`#c0d070`)
  for the one thing to do on each screen. The light theme uses warm paper (`#f4f1e8`) with navy (`#003050`) as the
  accent.
* No gradients and no two-tone fills on buttons, toggles or selections.
* Square-ish, not square: 6 px corners on controls, 8 px on panels. Icons and labels centred on their boxes.
* Space Grotesk (or Inter) for text, IBM Plex Mono (or JetBrains Mono) for code, plans and logs, falling back to
  the system fonts.
* The security stack (nested boxes, outermost layer first, ending in the command) is the signature element: it appears
  wherever a service or plugin is described.

## The screens

| # | Screen |
| ---: | --- |
| 1 | [Main window](screens/final-01-main.png): layer rings, chips, security stack, console |
| 2 | [Plan and drift](screens/final-02-plan-drift.png): the plan sheet, *Needs you*, Try in a copy |
| 3 | [New service](screens/final-03-new-service.png): folder, Fit check, mode, traffic |
| 4 | [Network settings](screens/final-04-network.png): traffic out, allowed hosts, ports in |
| 5 | [Editor and AI](screens/final-05-editor-ai.png): ruff problems, snapshots, reviewed AI edits |
| 6 | [Plugins and permissions](screens/final-06-plugins.png): grants, hash, calls, safe mode |
| 7 | [Installing a plugin](screens/final-07-plugin-install.png): installed off, no permissions, its jail |
| 8 | [Engines](screens/final-08-engines.png): Podman, Docker, WSL |
| 9 | [Security timeline](screens/final-09-security.png): Guard rules and the event timeline |
| 10 | [System check](screens/final-10-system-check.png): measured on this machine |
| 11 | [Light theme, command palette](screens/final-11-light-palette.png) |
| 12 | [Responsive layouts](screens/final-12-responsive.png): the window at narrower widths |

Screenshots of the real 0.0.1 window are in [`screenshots/`](../../screenshots/).
