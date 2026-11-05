# Security policy

## Supported versions

Security fixes go into the latest version only. Please update before reporting.

## Reporting a vulnerability

Report privately through GitHub's
**[Report a vulnerability](https://github.com/cfunkz/Pythonator/security/advisories/new)** form (repository →
Security → Advisories), never in a public issue or discussion. In the app, **Support → Report a security problem
privately** opens it. If you can't use GitHub, email [hello@routezero.tech](mailto:hello@routezero.tech).

Please include the version (`pythonator --version`), the operating system, the isolation mode (Sandbox, Container
or Full access) and the steps to reproduce. **System check** lists what the sandbox enforces on your computer.

Of particular interest:

* a way out of the sandbox or a container, or to a port the service didn't list;
* a service or plugin that reads Pythonator's data folder or the background service's token;
* a third-party plugin that gets more than it was granted, acts outside test mode, or passes as official;
* another local account controlling your background service;
* input that reaches a program, git or a container engine without validation.

You'll get an answer within a week. When a fix is released, the advisory is published with credit to you unless you
prefer otherwise.

The security model, per operating system, is in [docs/security.md](docs/security.md).
