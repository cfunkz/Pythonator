# Networking

Two separate questions: can the service **send** traffic out, and can anything **reach** it through a port?

## Traffic out

| Setting | Effect |
| --- | --- |
| **Off** (default) | no network at all, not even DNS |
| **Allowed hosts** | only the hosts you list (`api.example.com`, `*.example.com`, `example.com:8443`), through Pythonator's egress gate |
| **Anywhere** | the internet; your local network only if you also turn on *Allow local network* |

How each mode does it:

| Mode | Off | Allowed hosts | Anywhere |
| --- | --- | --- | --- |
| Sandbox, Linux | own network namespace with no interface, seccomp refuses sockets | own namespace; `HTTP_PROXY`/`HTTPS_PROXY` point at the gate's relay inside the isolated environment | own namespace through `pasta --no-map-gw`; cloud metadata and (unless allowed) the LAN are blackholed |
| Sandbox, macOS | Seatbelt denies the network | Seatbelt allows only the gate on 127.0.0.1 | Seatbelt allows outbound, not 127.0.0.1 |
| Sandbox, Windows | no network capability in the AppContainer | refused (`NET-WIN-001`): use WSL mode | `internetClient` capability; loopback stays closed |
| Container | `--network none` | refused (`NET-CTR-001`): the engine owns the network | a network of its own, `pythonator-<name>` |
| WSL | the isolated environment's own namespace | the gate inside Pythonator's distribution | WSL's network, isolated |
| Full access | not enforced | refused (`NET-FULL-001`): nothing could enforce it | as you |

The egress gate checks each connection's host name against the list, resolves it itself, and refuses private,
loopback and metadata addresses unless you allowed the local network. Every refusal is listed on the service's
**Network** tab with an **Allow** button, which adds the host to the list (a widening you approve). Linux needs the
`passt` package for traffic; without it the start is refused with `NET-LNX-001`.

Programs listening on your computer's `127.0.0.1` (databases, dev servers, admin tools, the Docker API on 2375) are
**not** reachable from Sandbox, Container or WSL services in any traffic setting. In Full access they are.

## Ports in

Open a port (1024 to 65535) and choose who can reach it:

| Reach | Who |
| --- | --- |
| **This computer** | programs on this computer only (`127.0.0.1`) |
| **Local network** | other devices on your network too |

Pick it in the **Ports in** step of New service, or in Settings → Network.

The service gets `PORT` (the first port) and `HOST`. In a private network namespace (Linux Sandbox, Container, WSL)
`HOST` is `0.0.0.0`, which is only the isolated environment's own address; on macOS and in Full access it is `127.0.0.1` unless a
port is open to the local network. A variable you set yourself wins.

Before a start, each port is tried: a busy one blocks the start with `NET-PORT-001` and a free port nearby.

Per mode: Linux Sandbox forwards the port into the isolated environment (`pasta -t`, or the gate's listener when traffic is Off);
macOS allows `bind` only on the listed ports; Container publishes `-p 127.0.0.1:P:P` (or all interfaces for the
local network); WSL uses WSL's own forwarding, to this computer only (`NET-WSL-002`, `NET-WSL-003`). **Windows
Sandbox can't open ports** (`NET-WIN-002`): an AppContainer server isn't reachable without firewall changes that
need an administrator, so Pythonator refuses rather than promise it. Use WSL or Container mode for a server on
Windows.

Opening a port, or widening it from This computer to Local network, is a widening: the plan requires your approval again.
Guard can refuse LAN ports or ports outside a range (see [Plugins](plugins.md#guard)).
