# Security policy

## Supported versions

Only the latest 0.0.x release receives security fixes.

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** form (repository → Security → Advisories), not in a
public issue. If GitHub is not an option, email [hello@routezero.tech](mailto:hello@routezero.tech).

Please include the version (`pythonatorctl version`), the OS and kernel, the mode (Sandbox, Container, WSL or
Full access), steps to reproduce, and a screenshot of System check.

Reports in these areas are especially useful:

* a Sandbox service reading or writing outside its folder, or reaching the network when Traffic is Off;
* a service reaching programs on the host's 127.0.0.1;
* a service starting with a layer weaker than its plan says, or starting without your approval;
* a change that widens access without a new approval (risk-hash bypass);
* another local account, or a service, talking to `pythonatord`;
* a secret reaching a log file unmasked;
* Pythonator stopping or removing a container or process it didn't create;
* a plugin reading files, reaching the network, or calling a method it wasn't granted;
* grants surviving a change to a plugin's code, or a `policy.check` plugin failing open.

Reports are acknowledged within a week. The advisory is published with the fix and credits the reporter unless
they ask otherwise.

See [docs/security.md](docs/security.md) for the security model, [docs/isolation.md](docs/isolation.md#known-limitations)
for the known limitations, and [docs/security-cases.md](docs/security-cases.md) for every case from earlier audits.
