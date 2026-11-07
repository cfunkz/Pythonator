"""Container mode: Docker Compose projects and their safety check, the default images, and folders it may not mount."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pythonator import isolation
from pythonator.core import runtimes
from pythonator.core.models import Service
from pythonator.isolation import IsolationError, compose, container
from pythonator.paths import DataDirs


def compose_service(folder: Path, **kw: Any) -> Service:
    return Service.model_validate({"id": "abc123", "name": "stack", "folder": str(folder), "runtime": "compose",
                                   "entry": "compose.yaml", "mode": "container", **kw})


def test_compose_runs_in_container_mode_only(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    assert runtimes.detect(tmp_path)[0] is runtimes.RUNTIMES["compose"]
    assert runtimes.detect(tmp_path)[1] == "compose.yaml"
    with pytest.raises(ValueError, match="Container mode"):
        Service.model_validate({"name": "s", "folder": str(tmp_path), "runtime": "compose", "mode": "sandbox"})
    assert compose_service(tmp_path).mode.value == "container"


def test_compose_audit_refuses_what_a_container_may_not_have(tmp_path: Path) -> None:
    folder = tmp_path.resolve()
    config = {"services": {
        "ok": {"image": "nginx", "ports": [{"target": 80, "published": "8080", "host_ip": "127.0.0.1"}],
               "volumes": [{"type": "bind", "source": str(folder / "data"), "target": "/data"},
                           {"type": "volume", "source": "db", "target": "/db"}],
               "build": {"context": str(folder)}, "cap_add": ["NET_BIND_SERVICE"], "network_mode": "service:x"},
        "bad": {"privileged": True, "network_mode": "host", "pid": "container:other", "cap_add": ["SYS_ADMIN"],
                "devices": ["/dev/sda"], "security_opt": ["seccomp=unconfined"],
                "volumes": [{"type": "bind", "source": "/etc", "target": "/x"},
                            {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}],
                "build": {"context": str(folder.parent)}, "env_file": [{"path": str(folder.parent / "x.env")}],
                "ports": [{"target": 80, "published": "8000"}]},
    }, "secrets": {"pw": {"file": str(folder.parent / "pw.txt")}}}
    problems = compose.audit(config, folder, lan=False)
    assert not [p for p in problems if p.startswith("ok:")], problems
    joined = "\n".join(problems)
    for expected in ("privileged", "network_mode: host", "pid: container:other", "SYS_ADMIN", "devices",
                     "seccomp=unconfined", "mounts /etc", "container engine", "builds from", "env file",
                     "publishes port 8000 to every network", "secrets pw"):
        assert expected in joined, expected
    assert not [p for p in compose.audit(config, folder, lan=True) if "publishes" in p]  # chosen: other computers
    assert compose.published(config) == [8080, 8000]


def test_compose_shell_and_unknown_steps_are_refused(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    service = compose_service(tmp_path)
    with pytest.raises(IsolationError, match="shell"):
        compose.wrap(service, "/usr/bin/docker", task=["/bin/sh", "-c", "id"])
    with pytest.raises(IsolationError, match="shell"):
        compose.wrap(service, "/usr/bin/docker", task=[*compose.base("/usr/bin/docker", service), "exec", "x"])
    down = compose.steps("/usr/bin/docker", service, "down")[0]
    cmd = compose.wrap(service, "/usr/bin/docker", task=down)
    assert cmd.argv[-2:] == ["down", "--remove-orphans"] and "-p" in cmd.argv and "pythonator-abc123" in cmd.argv
    with pytest.raises(IsolationError, match="network on"):
        compose.check("/usr/bin/docker", compose_service(tmp_path, network={"enabled": False}))


def test_compose_cli_keeps_what_it_needs_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")  # where Docker Desktop keeps the compose plugin
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "mine")
    env = compose.environment(compose_service(tmp_path, env={"WEB_PORT": "1"}))
    assert env["PROGRAMFILES"] == r"C:\Program Files" and env["WEB_PORT"] == "1" and env["COMPOSE_MENU"] == "false"
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_compose_file_must_be_in_the_folder(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    service = compose_service(tmp_path, entry="../compose.yaml")
    with pytest.raises(IsolationError, match="isn't a file in the service's folder"):
        compose.compose_file(service)


def compose_version() -> tuple[int, ...]:
    """The installed Docker Compose's version, () without one."""
    docker = shutil.which("docker")
    if not docker:
        return ()
    done = subprocess.run([docker, "compose", "version", "--short"], capture_output=True, text=True, check=False)
    numbers = done.stdout.strip().lstrip("v").split("-")[0].split(".") if done.returncode == 0 else []
    return tuple(int(n) for n in numbers if n.isdigit())


HAS_COMPOSE = compose_version() >= (2, 24)  # what `compose config --no-env-resolution` needs


@pytest.mark.skipif(not HAS_COMPOSE, reason="Docker Compose 2.24 or newer isn't installed")
def test_compose_check_reads_the_file_as_the_engine_does(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web:\n    image: nginx\n    ports: ['127.0.0.1:${WEB_PORT:-8080}:80']\n"
        "  db:\n    image: postgres\n    volumes: ['/:/host']\n")
    service = compose_service(tmp_path, env={"WEB_PORT": "9123"})
    config = compose.read_config(shutil.which("docker") or "docker", service)
    assert compose.published(config) == [9123]  # the service's variables fill the file's ${…}
    with pytest.raises(IsolationError, match="mounts /, outside the project folder"):
        compose.check(shutil.which("docker") or "docker", service)


def test_compose_audit_sees_bind_mounts_by_another_name(tmp_path: Path) -> None:
    config: dict[str, Any] = {
        "services": {"app": {"image": "x", "volumes": [{"type": "volume", "source": "data", "target": "/d"}],
                             "networks": {"outside": None}}},
        "volumes": {"data": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/root"}}},
        "networks": {"outside": {"name": "host", "external": True}},
    }
    problems = compose.audit(config, tmp_path, lan=False)
    assert any("mounts /root" in p for p in problems) and any("host network" in p for p in problems)


ROOT = Path(__file__).resolve().parent.parent


def test_default_images_are_current_and_exist() -> None:
    images = {k: r.image for k, r in runtimes.RUNTIMES.items()}
    assert images["deno"] == "denoland/deno:debian"  # there is no bare "2" tag
    assert images["go"] == "golang:1.27"  # 1.24 ended on 2026-02-10
    assert images["dotnet"].endswith("sdk:10.0")  # .NET 9 ends on 2026-11-10
    assert images["node"] == "node:24-slim"
    docs = (ROOT / "docs" / "languages.md").read_text(encoding="utf-8")
    for image in images.values():
        if image:
            assert f"`{image}`" in docs or "compose" in image


def test_container_caches_live_in_the_project_and_php_installs_with_composer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "engine", lambda _choice="auto", _distro="": "/usr/bin/docker")
    for rt, var in (("go", "GOMODCACHE"), ("rust", "CARGO_HOME"), ("ruby", "BUNDLE_PATH"), ("deno", "DENO_DIR"),
                    ("dotnet", "NUGET_PACKAGES")):
        service = Service.model_validate({"name": rt, "folder": str(tmp_path), "runtime": rt, "entry": "x",
                                          "mode": "container"})
        cmd = container.wrap(service)
        assert cmd.env[var].startswith("/app/.container-cache/"), rt
        assert ["-e", var] == cmd.argv[cmd.argv.index(var) - 1:cmd.argv.index(var) + 1]
    php = Service.model_validate({"name": "php", "folder": str(tmp_path), "runtime": "php", "entry": "index.php",
                                  "mode": "container"})
    assert "composer:2" in container.wrap(php, task=["composer", "install"]).argv
    assert "php:8.5-cli" in container.wrap(php).argv
    mine = php.model_copy(update={"env": {"BUNDLE_PATH": "x"}, "runtime": "ruby"})
    assert container.wrap(mine).env["BUNDLE_PATH"] == "x"  # the service's own value wins


def test_container_mode_refuses_your_home_and_pythonators_data(dirs: DataDirs) -> None:
    for folder in (Path.home(), dirs.root):
        service = Service.model_validate({"name": "c", "folder": str(folder), "entry": "app.py", "mode": "container"})
        with pytest.raises(isolation.IsolationError, match="container service can't use"):
            isolation.prepare(service, dirs)
