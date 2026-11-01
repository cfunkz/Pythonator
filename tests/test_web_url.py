"""The 🌐 Open button: finding a web server's address for any language, also for $ console commands."""

from __future__ import annotations

import random
import socket
import sys
from pathlib import Path

import pytest

from pythonator.core.logs import ADDRESS_HINT_LINES, LogStore
from pythonator.core.models import RestartPolicy, ServiceConfig
from pythonator.core.paths import DataPaths
from pythonator.core.stats import HAS_PSUTIL, listening_sockets
from pythonator.core.urls import address_url, browser_host, listening_url, local_url, url_port
from pythonator.supervisor import Status, Supervisor

from .conftest import wait_until

needs_psutil = pytest.mark.skipif(not HAS_PSUTIL, reason="psutil not installed")


def free_port() -> int:
    """A free port below the ephemeral range (the listening-port fallback ignores random high ports)."""
    for _ in range(200):
        port = random.randint(20000, 30000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port")


# -- what servers print -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "url"),
    [
        # full links (Python, Node, Rust, .NET, PHP, Deno, Ruby, Kotlin, Elixir...)
        (" * Running on http://127.0.0.1:8765/   (the 🌐 Open button should appear)", "http://127.0.0.1:8765/"),
        ("\x1b[1;32m * Running on http://127.0.0.1:8765/\x1b[0m", "http://127.0.0.1:8765/"),
        ("Now listening on: http://localhost:5000", "http://localhost:5000"),
        ("[Thu Nov  1 15:41:17 2026] PHP 8.3 Development Server (http://localhost:8000) started",
         "http://localhost:8000"),
        ("Listening on http://[::1]:8000/", "http://[::1]:8000/"),
        ("Rocket has launched from http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("Responding at http://0.0.0.0:8080", "http://localhost:8080"),
        ("Gradio: Running on local URL:  http://127.0.0.1:7860", "http://127.0.0.1:7860"),
        ("Bound to http://127.0.1.1:9000/api", "http://127.0.1.1:9000/api"),
        ("POST https://discord.com/api/v10/channels 200", None),
        ("Running on http://203.0.113.9:5000", None),  # someone else's machine
    ],
)
def test_printed_links(line: str, url: str | None) -> None:
    assert local_url(line) == url


@pytest.mark.parametrize(
    ("line", "url"),
    [
        ("[GIN-debug] Listening and serving HTTP on :8080", "http://localhost:8080/"),  # Go
        ("2026/11/01 15:41:17 Server listening on :3000", "http://localhost:3000/"),  # Go net/http
        ("Example app listening on port 3000", "http://localhost:3000/"),  # Node/Express
        ("Server is running on port 4000", "http://localhost:4000/"),
        ("Tomcat started on port(s): 8080 (http) with context path ''", "http://localhost:8080/"),  # Spring
        ("Tomcat started on port 8443 (https)", "https://localhost:8443/"),
        ("Netty started on port 8081", "http://localhost:8081/"),
        ("listening on 127.0.0.1:3000", "http://127.0.0.1:3000/"),  # Rust axum
        ("listening on [::]:8080", "http://localhost:8080/"),
        ("* Listening on tcp://0.0.0.0:3000", "http://localhost:3000/"),  # Ruby Puma
        ("[info] Running MyAppWeb.Endpoint with Bandit 1.5 at 127.0.0.1:4000 (http)", "http://127.0.0.1:4000/"),
        ("Server ready at 0.0.0.0:9090", "http://localhost:9090/"),
        ("\x1b[32mListening on port \x1b[1m5050\x1b[0m", "http://localhost:5050/"),
        ("PORT=8765 server ready", "http://localhost:8765/"),
        # not a server address
        ("connected to redis at localhost:6379", None),
        ("Connecting to postgres on port 5432, server ready", None),
        ("Server started at 15:41:17", None),
        ("ready in 0:05", None),
        ("Listening on api.example.com:443", None),
        ("Server running on 203.0.113.9:8000", None),
        ("sent 3000 messages to port", None),
        ("just a log line with :8080 in it", None),
        ("", None),
    ],
)
def test_bare_addresses(line: str, url: str | None) -> None:
    assert address_url(line) == url


def test_browser_host() -> None:
    assert browser_host("0.0.0.0") == "localhost"
    assert browser_host("[::]") == "localhost"
    assert browser_host("") == "localhost"
    assert browser_host("127.0.0.1") == "127.0.0.1"
    assert browser_host("::1") == "[::1]"
    assert browser_host("app.localhost") == "app.localhost"
    assert browser_host(socket.gethostname()) == socket.gethostname().lower()
    assert browser_host("203.0.113.9") is None
    assert browser_host("example.com") is None


def test_listening_url_picks_the_app_port() -> None:
    assert listening_url([]) is None
    assert listening_url([("0.0.0.0", 8000), ("::", 8000), ("127.0.0.1", 9229)]) == "http://localhost:8000/"
    assert listening_url([("127.0.0.1", 5000)]) == "http://127.0.0.1:5000/"
    assert listening_url([("::1", 5000)]) == "http://[::1]:5000/"
    assert listening_url([("127.0.0.1", 5000), ("::", 5000)]) == "http://localhost:5000/"
    assert listening_url([("127.0.0.1", 45123)]) is None  # random IPC/debug port
    assert listening_url([("203.0.113.9", 80)]) is None
    assert listening_url([("0.0.0.0", 443)]) == "https://localhost:443/"


def test_url_port() -> None:
    assert url_port("http://localhost:8080/x") == 8080
    assert url_port("http://localhost/") == 80
    assert url_port("https://localhost/") == 443


# -- the log store ------------------------------------------------------------------------------------


def test_printed_link_beats_an_earlier_bare_address(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    stream = store.stream()
    stream.write("Server listening on port 3000\n")
    assert store.url == "http://localhost:3000/" and not store.url_exact
    stream.write("Docs at http://localhost:3000/docs\nalso http://localhost:9999/\n")
    assert store.url == "http://localhost:3000/docs" and store.url_exact
    store.reset_url()
    assert store.url is None and not store.url_exact
    stream.close()
    store.close()


def test_bare_addresses_only_looked_for_after_a_start(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "x.log")
    stream = store.stream()
    stream.write("tick\n" * ADDRESS_HINT_LINES + "Server listening on port 3000\n")
    assert store.url is None
    stream.write("Running on http://127.0.0.1:5000\n")  # full links are always recognised
    assert store.url == "http://127.0.0.1:5000"
    store.reset_url()
    stream.write("Server listening on port 3000\n")
    assert store.url == "http://localhost:3000/"
    stream.close()
    store.close()


@needs_psutil
def test_listening_sockets_of_this_process() -> None:
    import psutil

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert ("127.0.0.1", port) in listening_sockets([psutil.Process()])


# -- the supervisor -----------------------------------------------------------------------------------

SERVER = """\
import sys, time
from http.server import HTTPServer, BaseHTTPRequestHandler
port = int(sys.argv[1])
httpd = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
{announce}
sys.stdout.flush()
httpd.serve_forever()
"""


def _server(tmp_path: Path, name: str, announce: str) -> Path:
    p = tmp_path / name
    p.write_text(SERVER.format(announce=announce))
    return p


def test_open_shows_for_a_server_started_with_a_dollar_command(tmp_path: Path, qapp: object) -> None:
    port = free_port()
    script = _server(tmp_path, "srv.py", 'print(f" * Running on http://127.0.0.1:{port}/")')
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="web", target=str(script), python=sys.executable,
                                restart=RestartPolicy.NEVER))
    assert svc.status is Status.STOPPED
    assert sup.run_shell(svc.id, f'"{sys.executable}" "{script}" {port}')
    assert wait_until(lambda: sup.web_url(svc) is not None, timeout=15, qt=True), \
        [x.text for x in svc.log.snapshot()]
    assert sup.web_url(svc) == f"http://127.0.0.1:{port}/"
    sup.stop(svc.id)
    assert wait_until(lambda: svc.task is None, timeout=15, qt=True)
    assert sup.web_url(svc) is None  # gone with the command
    sup.shutdown()


def test_a_new_dollar_command_forgets_the_previous_address(tmp_path: Path, qapp: object) -> None:
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="web", target="x.py", workdir=str(tmp_path), python=sys.executable))
    svc.log.stream().write("Running on http://127.0.0.1:5000\n")
    assert svc.log.url
    assert sup.run_shell(svc.id, f'"{sys.executable}" -c "import time; time.sleep(5)"')
    assert svc.log.url is None
    sup.shutdown()


@needs_psutil
def test_open_shows_for_a_server_that_prints_nothing(tmp_path: Path, qapp: object) -> None:
    port = free_port()
    script = _server(tmp_path, "quiet.py", "")
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="quiet", target=str(script), args=str(port), python=sys.executable,
                                restart=RestartPolicy.NEVER))
    assert sup.start(svc.id)
    assert wait_until(lambda: sup.web_url(svc) is not None, timeout=15, qt=True)
    assert sup.web_url(svc) == f"http://127.0.0.1:{port}/"
    assert svc.log.url is None  # found from the listening socket, not the output
    sup.shutdown()


@needs_psutil
def test_a_wrong_bare_address_is_corrected_by_the_listening_port(tmp_path: Path, qapp: object) -> None:
    port = free_port()
    script = _server(tmp_path, "liar.py", 'print("server ready on port 1234")')
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="liar", target=str(script), args=str(port), python=sys.executable,
                                restart=RestartPolicy.NEVER))
    assert sup.start(svc.id)
    assert wait_until(lambda: sup.web_url(svc) == f"http://127.0.0.1:{port}/", timeout=15, qt=True), \
        (svc.log.url, sup.web_url(svc))
    sup.shutdown()


def test_open_button_visible_for_a_dollar_command(tmp_path: Path, qapp: object) -> None:
    from pythonator.ui import theme
    from pythonator.ui.main_window import MainWindow

    theme.apply_theme(qapp)  # type: ignore[arg-type]
    port = free_port()
    script = _server(tmp_path, "srv.py", 'print(f"Listening on http://localhost:{port}")')
    sup = Supervisor(DataPaths(tmp_path / "data"))
    svc = sup.add(ServiceConfig(name="web", target=str(script), python=sys.executable))
    window = MainWindow(sup)
    window.show()
    window.select(svc.id)
    assert not window.btn_web.isVisible()
    window.console.mode.setCurrentIndex(1)
    window._on_console_submit("shell", f'"{sys.executable}" "{script}" {port}')
    assert wait_until(lambda: window.btn_web.isVisible(), timeout=15, qt=True)
    assert f"localhost:{port}" in window.btn_web.toolTip()
    sup.stop(svc.id)
    assert wait_until(lambda: not window.btn_web.isVisible(), timeout=15, qt=True)
    window.quit()
    assert wait_until(lambda: not window.isVisible(), qt=True)
