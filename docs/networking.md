# Ports and APIs

A web server, an API or a webhook receiver needs a port that other programs can connect to. Pythonator lets a
service listen on the ports you list, and only those, and lets you choose who may connect.

## Serving a port

Open the service's **Network** tab:

| Setting | Meaning |
| --- | --- |
| May use the network | Off: no network at all, not even outgoing connections. On: outgoing connections allowed |
| Serves on ports | The ports it listens on, for example `8000` or `8000, 8443` (up to 32) |
| Who may connect | **This computer only**: your browser and your other programs. **Other computers on home or work networks too**: your phone, your other machines |

Pythonator passes the first port to the program as `PORT`, and with *This computer only* also `HOST=127.0.0.1`,
unless you set them yourself. Most frameworks read them, or you can:

```python
# Python (FastAPI with uvicorn)
import os, uvicorn
uvicorn.run("app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ["PORT"]))
```

```js
// Node.js (Express)
app.listen(Number(process.env.PORT), process.env.HOST ?? "127.0.0.1");
```

```go
// Go
http.ListenAndServe(os.Getenv("HOST")+":"+os.Getenv("PORT"), mux)
```

When the service runs, the header shows a link to `http://127.0.0.1:PORT/` and the list of ports it actually listens
on. The [Notify](plugins.md#notify) plugin calls it every few seconds and shows whether it answers.

## What each mode enforces

| | Sandbox: Linux | Sandbox: macOS | Sandbox: Windows | Container |
| --- | --- | --- | --- | --- |
| Only the listed ports | Yes, Linux 6.7+ (Landlock TCP rules) | Yes | Yes, through the firewall rules of Make reachable | Yes: only those are published |
| This computer only | The program's choice: Pythonator sets `HOST=127.0.0.1` | Yes | Yes (firewall rule for `127.0.0.1`) | Yes (`-p 127.0.0.1:PORT:PORT`) |
| Network off | Yes (seccomp: no sockets) | Yes | Yes (no network capability) | Yes (`--network none`) |

On Linux, a sandboxed server reached by other computers is also subject to your firewall (`ufw`, `firewalld`).

## Windows: Make reachable

Windows keeps every AppContainer unreachable, even from the same computer, until an administrator allows it. So on
Windows the Network tab has **Make reachable…** and **Undo**. Make reachable asks for administrator rights once,
then:

1. adds a loopback exemption for this service's AppContainer (`CheckNetIsolation LoopbackExempt`), so programs on
   this computer can connect;
2. adds a Windows Filtering Platform permit with the same scope (this AppContainer, a loopback connection from
   `127.0.0.1`, the listed ports). Windows' own block of loopback connections *into* an AppContainer sits where
   firewall rules can't override it, and the loopback exemption alone only lets the container connect *out*. The
   permit sits just above that block, for this one container, and stays until Undo. This is what lets your browser
   open `http://127.0.0.1:<port>/`;
3. adds an inbound firewall rule for this service only, for TCP from `127.0.0.1`, on the listed ports;
4. with *Other computers* chosen, adds a second rule for the listed ports on private and domain networks (never
   public Wi-Fi).

Every rule is tied to the service's own AppContainer, in a **Pythonator** firewall group, so no other program gains
anything. **Undo** removes the exemption, the permit and both rules. **Removing** a service does the same: when it was
made reachable, Windows asks for administrator rights once more and the rules go with it. If you cancel that prompt
(or removed the service with an older Pythonator), nothing listens behind the rules any more, but they stay in the
firewall list and would admit whatever later runs under that AppContainer's name: **System check** lists them under
*Left behind by services* and **Clean up…** removes them.

Run it again after you change the ports. Without it, the service runs and listens, but connections time out.

You don't have to look for it. A sandboxed server's start says when Windows still keeps it out of reach, and the
**Open** button (open in your browser) offers Make reachable first, then opens the page. That is the same single
administrator prompt. Pythonator reads the list of exemptions with `CheckNetIsolation LoopbackExempt -s`, which
needs no administrator rights.

## Connecting to other containers

A service in Container mode can join a container network, to reach a database or a cache running in another
container by name.

1. Create a network and start the database on it, once:

   ```bash
   docker network create appnet
   docker run -d --name db --network appnet -e POSTGRES_PASSWORD=change-me postgres:17
   ```

2. In the service's **Isolation** tab, choose **Container** and type `appnet` under **Network**.
3. In the program, connect to `db:5432`. Put the password in the service's environment or `.env` file.

The network can be any name except `host`, which Pythonator refuses because it would remove the isolation.

Inside the container, the program should listen on `0.0.0.0`: the port is published to the host on `127.0.0.1` (this
computer) or all addresses (other computers) by the engine, according to *Who may connect*.

## Connecting from a sandbox to a container

A sandboxed service with the network on can connect to a container's published port like any other program, for
example a database started with `-p 127.0.0.1:5432:5432` is at `127.0.0.1:5432`.

## Calling Pythonator itself

Pythonator's own background service isn't on the network at all. It listens on a local socket that only your
account can open, with a token. Scripts of yours can use it: see [Background service API](service-api.md).
