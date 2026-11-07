# Security policy

## Supported versions

Only the latest release receives security fixes. Please check that the problem still occurs on it before reporting.

## Reporting a vulnerability

Report privately through GitHub's
**[Report a vulnerability](https://github.com/cfunkz/Pythonator/security/advisories/new)** form (repository →
Security → Advisories), not in a public issue or discussion. In the app, **Help ▾ → Support → Report a security
problem privately** opens the same form. If GitHub is not an option, email
[hello@routezero.tech](mailto:hello@routezero.tech).

Please include:

* the version (`pythonator --version`) and operating system;
* the isolation mode (Sandbox, Container or Full access);
* steps to reproduce;
* the output of **System check**, which lists the sandbox protections enforced on that computer.

Reports in these areas are particularly useful:

* escaping the sandbox or a container, or listening on a port the service did not declare;
* a service or plugin reading Pythonator's data folder or the background service's token;
* a third-party plugin obtaining more than it was granted, acting outside test mode, or passing as official;
* another local account controlling the background service;
* input reaching a program, git or a container engine without validation.

Reports are acknowledged within a week. When a fix is released, the advisory is published and credits the reporter
unless they prefer otherwise.

The security model for each operating system is described in [docs/security.md](docs/security.md).
