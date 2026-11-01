"""Every supported runtime in one table: how to find it, launch a script or entry point, and install packages.

Adding a language is one :class:`RuntimeInfo` entry plus a :class:`~.models.Runtime` member.
The builders here only turn an already resolved :class:`Ctx` into an ``argv``; finding the
working directory, environment and interpreter is done once, the same way for everyone, in
:mod:`.launch`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache, cached_property
from pathlib import Path

from .errors import LaunchError
from .macos import bundle_executable
from .models import LaunchMode, Runtime, ServiceConfig
from .paths import IS_MAC, IS_WINDOWS


def path_of(env: Mapping[str, str]) -> str | None:
    return next((v for k, v in env.items() if k.upper() == "PATH"), None)


def split_options(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=not IS_WINDOWS) if text.strip() else []
    except ValueError as exc:
        raise LaunchError(f"Could not parse the runtime options: {exc}") from exc


class Ctx:
    """Everything a builder needs. The runtime's executable is looked up only if a builder asks."""

    def __init__(
        self,
        cfg: ServiceConfig,
        workdir: Path,
        env: Mapping[str, str],
        target: str,  # script path (resolved) or entry point name, depending on the mode
        args: list[str],
        options: list[str],  # runtime options, placed before the script
        find_program: Callable[[], str],
    ) -> None:
        self.cfg, self.workdir, self.env = cfg, workdir, env
        self.target, self.args, self.options = target, args, options
        self._find_program = find_program

    @cached_property
    def program(self) -> str:
        """The runtime's own executable ("" for plain programs). Raises :class:`LaunchError`."""
        return self._find_program()

    @property
    def resolved_program(self) -> str:
        """The executable if a builder already looked it up, else ""."""
        return str(self.__dict__.get("program", ""))

    def optional_program(self) -> str:
        try:
            return self.program
        except LaunchError:
            return ""

    def tool(self, *names: str, hint: str = "") -> str:
        """Find a companion tool (npm, bundle, composer, ...) next to the runtime or on PATH."""
        program = self.optional_program()
        folder = str(Path(program).parent) if program else ""
        search = os.pathsep.join(filter(None, [folder, path_of(self.env) or ""]))
        for name in names:
            if found := shutil.which(name, path=search):
                return found
        raise LaunchError(f"{names[0]} was not found on PATH." + (f" {hint}" if hint else ""))


@dataclass(frozen=True, slots=True)
class ModeInfo:
    label: str  # shown in the "Run" combo
    field: str  # form label of the target field
    hint: str  # placeholder of the target field


Builder = Callable[[Ctx], list[str]]
Installer = Callable[[Ctx, list[str]], list[list[str]]]


@dataclass(frozen=True)
class RuntimeInfo:
    runtime: Runtime
    programs: tuple[str, ...]  # executable names tried on PATH, in order; empty: the script is the program
    website: str
    modes: dict[LaunchMode, ModeInfo]
    script: Builder
    module: Builder | None = None
    install: Installer | None = None
    packages_hint: str = ""
    no_install: str = ""  # why "Install dependencies" doesn't apply
    options_hint: str = ""
    search_dirs: Callable[[], list[Path]] = field(default=lambda: [])
    skip: Callable[[str], bool] = field(default=lambda _p: False)  # reject a found program (e.g. WSL bash)
    manifests: tuple[str, ...] = ()  # dependency files, most specific first (globs allowed)

    @property
    def label(self) -> str:
        return self.runtime.label

    def manifest(self, workdir: Path) -> Path | None:
        """The project's dependency file (requirements.txt, package.json, go.mod, ...), if any."""
        for pattern in self.manifests:
            if any(c in pattern for c in "*?["):
                found = sorted(workdir.glob(pattern))
                if found:
                    return found[0]
            elif (workdir / pattern).is_file():
                return workdir / pattern
        return None

    def mode_label(self, mode: LaunchMode) -> str:
        info = self.modes.get(mode)
        return info.label if info else mode.value


def _command_mode(example: str) -> ModeInfo:
    return ModeInfo("Command", "Command", example)


def _home(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _env_dir(var: str, *parts: str) -> list[Path]:
    value = os.environ.get(var, "")
    return [Path(value).joinpath(*parts)] if value else []


def _program_files(*parts: str) -> list[Path]:
    roots = {os.environ.get(v, "") for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")}
    return [Path(r).joinpath(*parts) for r in sorted(roots) if r]


def _homebrew(*parts: str) -> list[Path]:
    """``/opt/homebrew/<parts>`` (Apple silicon) and ``/usr/local/<parts>`` (Intel) on macOS."""
    return [Path(root).joinpath(*parts) for root in ("/opt/homebrew", "/usr/local")] if IS_MAC else []


MAC_PYTHON_ORG = "/Library/Frameworks/Python.framework/Versions/Current/bin"  # python.org installer


def _run_ok(argv: list[str]) -> str | None:
    """stdout of a quick system query, or None if it fails."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


@cache
def mac_java_home() -> str | None:
    """The JDK macOS's /usr/bin/java would run (``/usr/libexec/java_home``), or None if there is none."""
    return _run_ok(["/usr/libexec/java_home"]) if IS_MAC else None


@cache
def mac_developer_tools() -> bool:
    """True when Apple's command line tools are installed (they provide /usr/bin/python3, git, ...)."""
    return IS_MAC and _run_ok(["/usr/bin/xcode-select", "-p"]) is not None


def is_mac_stub(path: str) -> bool:
    """macOS ships placeholders that only offer to install the real thing (with a dialog) when run."""
    if not IS_MAC:
        return False
    if path == "/usr/bin/java":
        return mac_java_home() is None
    if path in ("/usr/bin/python3", "/usr/bin/pip3"):
        return not mac_developer_tools()
    return False


# -- Python and Node.js -----------------------------------------------------------------------------


def _python_script(c: Ctx) -> list[str]:
    return [c.program, *c.options, "-u", c.target, *c.args]


def _python_module(c: Ctx) -> list[str]:
    return [c.program, *c.options, "-u", "-m", c.target, *c.args]


_LOCKFILES = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"))


def package_manager(workdir: Path, env: Mapping[str, str], program: str = "") -> str:
    """The package manager a Node project uses (by lockfile), falling back to npm."""
    search = os.pathsep.join(filter(None, [str(Path(program).parent) if program else "", path_of(env) or ""]))
    for lockfile, tool in _LOCKFILES:
        if (workdir / lockfile).is_file() and (found := shutil.which(tool, path=search)):
            return found
    if found := shutil.which("npm", path=search):
        return found
    raise LaunchError("npm was not found. Install Node.js from https://nodejs.org or set the node path.")


def _is_npm(pm: str) -> bool:
    return Path(pm).stem.lower() == "npm"


def _needs_package_json(c: Ctx) -> None:
    if not (c.workdir / "package.json").is_file():
        raise LaunchError(f"No package.json in {c.workdir}")


def _node_script(c: Ctx) -> list[str]:
    return [c.program, *c.options, c.target, *c.args]


def _node_module(c: Ctx) -> list[str]:
    _needs_package_json(c)
    pm = package_manager(c.workdir, c.env, c.optional_program())
    return [pm, "run", c.target, *(["--"] if c.args and _is_npm(pm) else []), *c.args]


def _node_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    pm = package_manager(c.workdir, c.env, c.optional_program())
    if packages:
        return [[pm, "install" if _is_npm(pm) else "add", *packages]]
    if (c.workdir / "package.json").is_file():
        return [[pm, "install"]]
    raise LaunchError("Nothing to install: add packages in the service settings, "
                      "or use Dependencies → Create package.json.")


# -- Bun and Deno ---------------------------------------------------------------------------------


def _bun_script(c: Ctx) -> list[str]:
    return [c.program, "run", *c.options, c.target, *c.args]


def _bun_module(c: Ctx) -> list[str]:
    _needs_package_json(c)
    return [c.program, "run", *c.options, c.target, *c.args]


def _bun_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    if packages:
        return [[c.program, "add", *packages]]
    if (c.workdir / "package.json").is_file():
        return [[c.program, "install"]]
    raise LaunchError("Nothing to install: add packages in the service settings, "
                      "or use Dependencies → Create package.json.")


def _deno_script(c: Ctx) -> list[str]:
    # Deno sandboxes by default; long-running services nearly always need network/files/env.
    # Runtime options replace the blanket -A, e.g. "--allow-net --allow-env".
    return [c.program, "run", *(c.options or ["-A"]), c.target, *c.args]


def _deno_module(c: Ctx) -> list[str]:
    if not any((c.workdir / f).is_file() for f in ("deno.json", "deno.jsonc", "package.json")):
        raise LaunchError(f"No deno.json or package.json with tasks in {c.workdir}")
    return [c.program, "task", c.target, *c.args]


def _deno_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    if packages:
        return [[c.program, "add", *packages]]
    if any((c.workdir / f).is_file() for f in ("deno.json", "deno.jsonc", "package.json")):
        return [[c.program, "install"]]
    raise LaunchError("Nothing to install: add packages (npm:express, jsr:@std/http), "
                      "or use Dependencies → Create deno.json.")


# -- compiled languages ---------------------------------------------------------------------------


def _go_script(c: Ctx) -> list[str]:
    return [c.program, "run", *c.options, c.target, *c.args]


def _go_module(c: Ctx) -> list[str]:
    return [c.program, "run", *c.options, c.target, *c.args]


def _go_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    if not (c.workdir / "go.mod").is_file():
        raise LaunchError(f"No go.mod in {c.workdir}. Use Dependencies → Create go.mod first.")
    if packages:
        return [[c.program, "get", *packages]]
    return [[c.program, "mod", "tidy"]]  # downloads what the code imports and records it in go.mod


# Java writes stdout in the platform encoding when it isn't a console; ask for UTF-8 (Java 19+,
# ignored by older versions).
JAVA_DEFAULTS = ("-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8")


def _java_script(c: Ctx) -> list[str]:
    suffix = Path(c.target).suffix.lower()
    if suffix == ".jar":
        return [c.program, *JAVA_DEFAULTS, *c.options, "-jar", c.target, *c.args]
    if suffix == ".java":  # single-file source launch (Java 11+)
        return [c.program, *JAVA_DEFAULTS, *c.options, c.target, *c.args]
    raise LaunchError("Choose a .jar file (or a single .java source file).")


def _java_module(c: Ctx) -> list[str]:
    return [c.program, *JAVA_DEFAULTS, *c.options, "-cp", ".", c.target, *c.args]


def _dotnet_script(c: Ctx) -> list[str]:
    suffix = Path(c.target).suffix.lower()
    if suffix == ".dll":
        return [c.program, *c.options, c.target, *c.args]
    if suffix == ".csproj":
        return [c.program, "run", "--project", c.target, *c.options, *(["--", *c.args] if c.args else [])]
    if suffix == ".cs":  # file-based apps (.NET 10+)
        return [c.program, "run", c.target, *c.options, *(["--", *c.args] if c.args else [])]
    raise LaunchError("Choose a .csproj project, a built .dll, or a single .cs file (.NET 10+).")


def _dotnet_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    if packages:
        return [[c.program, "add", "package", p] for p in packages]  # one package per call
    return [[c.program, "restore"]]


# -- scripting languages --------------------------------------------------------------------------


# Ruby buffers stdout when it isn't a terminal, so output would arrive in bursts. Turn on sync and
# run the script as the main program. `load` gives __FILE__ the expanded path (forward slashes on
# Windows), so $0 is expanded the same way to keep `if __FILE__ == $0` working.
RUBY_BOOT = "$stdout.sync = $stderr.sync = true; $0 = File.expand_path(ARGV.shift); load($0)"


def _ruby_script(c: Ctx) -> list[str]:
    return [c.program, *c.options, "-e", RUBY_BOOT, c.target, *c.args]


def _ruby_module(c: Ctx) -> list[str]:
    bundle = c.tool("bundle", hint="Install Bundler with `gem install bundler`.")
    return [bundle, "exec", *split_options(c.target), *c.args]


def _ruby_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    has_gemfile = (c.workdir / "Gemfile").is_file()
    if packages and not has_gemfile:
        return [[c.tool("gem"), "install", *packages]]
    bundle = c.tool("bundle", hint="Install Bundler with `gem install bundler`.")
    if packages:
        return [[bundle, "add", *packages]]
    if has_gemfile:
        return [[bundle, "install"]]
    raise LaunchError("Nothing to install: add gems in the service settings, "
                      "or use Dependencies → Create Gemfile.")


def _php_script(c: Ctx) -> list[str]:
    return [c.program, *c.options, c.target, *c.args]


def _php_module(c: Ctx) -> list[str]:
    return [c.program, *c.options, "-S", c.target, *c.args]


def _php_install(c: Ctx, packages: list[str]) -> list[list[str]]:
    composer = c.tool("composer", hint="Install Composer from https://getcomposer.org.")
    if packages:
        return [[composer, "require", *packages]]
    if (c.workdir / "composer.json").is_file():
        return [[composer, "install"]]
    raise LaunchError("Nothing to install: add packages in the service settings, "
                      "or use Dependencies → Create composer.json.")


def _shell_script(c: Ctx) -> list[str]:
    if Path(c.target).suffix.lower() in (".bat", ".cmd"):
        if not IS_WINDOWS:
            raise LaunchError("Batch files (.bat / .cmd) only run on Windows.")
        return [c.target, *c.args]  # CreateProcess runs batch files through cmd.exe itself
    return [c.program, *c.options, c.target, *c.args]


def _is_wsl_launcher(path: str) -> bool:
    # C:\Windows\System32\bash.exe starts WSL, which can't run a Windows path; prefer Git Bash.
    return IS_WINDOWS and Path(path).parent.name.lower() in ("system32", "windowsapps")


def _java_dirs() -> list[Path]:
    """JDKs that macOS's java_home knows about, then Homebrew's (keg-only) OpenJDK."""
    if not IS_MAC:
        return []
    home = mac_java_home()
    return [*((Path(home) / "bin",) if home else ()), *_homebrew("opt", "openjdk", "bin")]


def _powershell_script(c: Ctx) -> list[str]:
    return [c.program, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *c.options,
            "-File", c.target, *c.args]


def _program_script(c: Ctx) -> list[str]:
    path = Path(c.target)
    if path.suffix.lower() == ".app" and path.is_dir():  # a macOS app bundle: run the program inside
        inner = bundle_executable(path)
        if inner is None:
            raise LaunchError(f"No program found inside {path.name} (Contents/MacOS).")
        return [str(inner), *c.args]
    if not IS_WINDOWS and not os.access(path, os.X_OK):
        raise LaunchError(f"{path.name} is not executable. Run `chmod +x {path.name}` in $ shell mode.")
    return [c.target, *c.args]


# -- the table ------------------------------------------------------------------------------------

_PY = "Packages for 'Install dependencies', one per line (pip syntax).\n" \
      "Leave empty to use requirements.txt (or pyproject.toml) in the working dir."
_JS = "Packages for 'Install dependencies', one per line, e.g. discord.js or express@5.\n" \
      "Leave empty to install package.json (npm, pnpm, yarn or bun, by lockfile)."

RUNTIMES: dict[Runtime, RuntimeInfo] = {
    info.runtime: info
    for info in (
        RuntimeInfo(
            Runtime.PYTHON, ("python3", "python") if not IS_WINDOWS else ("python", "py"), "https://python.org",
            {
                LaunchMode.SCRIPT: ModeInfo("Python script", "Script", "path/to/bot.py"),
                LaunchMode.MODULE: ModeInfo("Python module (-m)", "Module",
                                            "e.g. uvicorn, http.server, mypackage.worker"),
                LaunchMode.COMMAND: _command_mode("e.g. gunicorn app:app -w 4, celery -A tasks worker"),
            },
            _python_script, _python_module, packages_hint=_PY, options_hint="e.g. -X dev, -O",
            manifests=("requirements.txt", "pyproject.toml"),
            search_dirs=lambda: [Path(MAC_PYTHON_ORG)] if IS_MAC else [], skip=is_mac_stub,
        ),
        RuntimeInfo(
            Runtime.NODE, ("node",), "https://nodejs.org",
            {
                LaunchMode.SCRIPT: ModeInfo("Node.js script", "Script", "path/to/bot.js  (or .mjs / .ts)"),
                LaunchMode.MODULE: ModeInfo("package.json script (npm run)", "npm script",
                                            "a script from package.json, e.g. start, dev"),
                LaunchMode.COMMAND: _command_mode("e.g. next start, nodemon server.js, pm2-runtime app.js"),
            },
            _node_script, _node_module, _node_install, packages_hint=_JS,
            options_hint="e.g. --max-old-space-size=4096 --enable-source-maps", manifests=("package.json",),
        ),
        RuntimeInfo(
            Runtime.BUN, ("bun",), "https://bun.sh",
            {
                LaunchMode.SCRIPT: ModeInfo("Bun script", "Script", "path/to/server.ts  (or .js)"),
                LaunchMode.MODULE: ModeInfo("package.json script (bun run)", "Script name", "e.g. start, dev"),
                LaunchMode.COMMAND: _command_mode("e.g. bunx some-cli"),
            },
            _bun_script, _bun_module, _bun_install,
            packages_hint="Packages for 'Install dependencies' (bun add), one per line.\n"
                          "Leave empty to run bun install for package.json.",
            options_hint="e.g. --smol, --watch", manifests=("package.json",),
            search_dirs=lambda: [_home(".bun", "bin"), *_env_dir("BUN_INSTALL", "bin")],
        ),
        RuntimeInfo(
            Runtime.DENO, ("deno",), "https://deno.com",
            {
                LaunchMode.SCRIPT: ModeInfo("Deno script", "Script", "path/to/main.ts"),
                LaunchMode.MODULE: ModeInfo("deno.json task (deno task)", "Task", "e.g. start, dev"),
                LaunchMode.COMMAND: _command_mode("e.g. deno serve main.ts"),
            },
            _deno_script, _deno_module, _deno_install,
            packages_hint="Packages for 'Install dependencies' (deno add), e.g. npm:express or jsr:@std/http.\n"
                          "Leave empty to run deno install.",
            options_hint="Permissions; default -A (all). e.g. --allow-net --allow-env",
            manifests=("deno.json", "deno.jsonc", "package.json"),
            search_dirs=lambda: [_home(".deno", "bin"), *_env_dir("DENO_INSTALL", "bin")],
        ),
        RuntimeInfo(
            Runtime.GO, ("go",), "https://go.dev/dl",
            {
                LaunchMode.SCRIPT: ModeInfo("Go file (go run)", "File", "path/to/main.go"),
                LaunchMode.MODULE: ModeInfo("Go package (go run)", "Package", "e.g. .  or ./cmd/bot"),
                LaunchMode.COMMAND: _command_mode("e.g. a built binary: ./bot -config prod.toml"),
            },
            _go_script, _go_module, _go_install,
            packages_hint="Modules for 'Install dependencies' (go get), e.g. github.com/bwmarrin/discordgo.\n"
                          "Leave empty to run go mod tidy.",
            options_hint="Build flags, e.g. -race or -tags prod", manifests=("go.mod",),
            search_dirs=lambda: [*_env_dir("GOROOT", "bin"), Path("/usr/local/go/bin"), _home("go", "bin"),
                                 *_program_files("Go", "bin"), *_homebrew("opt", "go", "bin")],
        ),
        RuntimeInfo(
            Runtime.JAVA, ("java",), "https://adoptium.net",
            {
                LaunchMode.SCRIPT: ModeInfo("Java .jar (or .java file)", "Jar", "path/to/server.jar"),
                LaunchMode.MODULE: ModeInfo("Main class (-cp .)", "Main class", "e.g. com.example.Bot"),
                LaunchMode.COMMAND: _command_mode("e.g. ./gradlew run"),
            },
            _java_script, _java_module,
            no_install="Java dependencies are handled by your build tool (Maven / Gradle). "
                       "Build the jar, then point the service at it.",
            options_hint="JVM options, e.g. -Xms1G -Xmx4G",
            manifests=("pom.xml", "build.gradle.kts", "build.gradle"),
            search_dirs=lambda: [*_env_dir("JAVA_HOME", "bin"), *_java_dirs()],
            skip=is_mac_stub,
        ),
        RuntimeInfo(
            Runtime.DOTNET, ("dotnet",), "https://dotnet.microsoft.com/download",
            {
                LaunchMode.SCRIPT: ModeInfo(".NET project / dll", "Project", "path/to/Bot.csproj, Bot.dll or app.cs"),
                LaunchMode.COMMAND: _command_mode("e.g. dotnet watch run"),
            },
            _dotnet_script, install=_dotnet_install,
            packages_hint="NuGet packages for 'Install dependencies' (dotnet add package), one per line.\n"
                          "Leave empty to run dotnet restore.",
            options_hint="e.g. -c Release (for projects)", manifests=("*.csproj", "*.fsproj"),
            search_dirs=lambda: [*_env_dir("DOTNET_ROOT"), _home(".dotnet"), *_program_files("dotnet"),
                                 *((Path("/usr/local/share/dotnet"),) if IS_MAC else
                                   () if IS_WINDOWS else (Path("/usr/share/dotnet"), Path("/usr/lib/dotnet")))],
        ),
        RuntimeInfo(
            Runtime.RUBY, ("ruby",), "https://www.ruby-lang.org/en/downloads/",
            {
                LaunchMode.SCRIPT: ModeInfo("Ruby script", "Script", "path/to/bot.rb"),
                LaunchMode.MODULE: ModeInfo("Bundler command (bundle exec)", "Command", "e.g. rails server, puma"),
                LaunchMode.COMMAND: _command_mode("e.g. rackup -p 9292"),
            },
            _ruby_script, _ruby_module, _ruby_install,
            packages_hint="Gems for 'Install dependencies', one per line (bundle add, or gem install without "
                          "a Gemfile).\nLeave empty to run bundle install.",
            options_hint="e.g. -W0, -I lib", manifests=("Gemfile",),
            search_dirs=lambda: _homebrew("opt", "ruby", "bin"),
        ),
        RuntimeInfo(
            Runtime.PHP, ("php",), "https://www.php.net/downloads",
            {
                LaunchMode.SCRIPT: ModeInfo("PHP script", "Script", "path/to/worker.php"),
                LaunchMode.MODULE: ModeInfo("Built-in web server (php -S)", "Address",
                                            "e.g. localhost:8000  (add -t public as an argument)"),
                LaunchMode.COMMAND: _command_mode("e.g. php artisan serve, php artisan queue:work"),
            },
            _php_script, _php_module, _php_install,
            packages_hint="Packages for 'Install dependencies' (composer require), one per line.\n"
                          "Leave empty to run composer install.",
            options_hint="e.g. -d memory_limit=512M", manifests=("composer.json",),
        ),
        RuntimeInfo(
            Runtime.SHELL, ("bash", "sh"), "https://git-scm.com/downloads (Git Bash, on Windows)",
            {
                LaunchMode.SCRIPT: ModeInfo("Shell script", "Script", "path/to/run.sh  (.bat / .cmd on Windows)"),
                LaunchMode.COMMAND: _command_mode("e.g. ./start.sh --prod"),
            },
            _shell_script, no_install="Shell scripts have no packages to install.",
            options_hint="e.g. -x to trace each command",
            search_dirs=lambda: _program_files("Git", "bin") if IS_WINDOWS else [],
            skip=_is_wsl_launcher,
        ),
        RuntimeInfo(
            Runtime.POWERSHELL, ("pwsh", "powershell"), "https://aka.ms/powershell",
            {
                LaunchMode.SCRIPT: ModeInfo("PowerShell script", "Script", "path/to/task.ps1"),
                LaunchMode.COMMAND: _command_mode("e.g. pwsh -Command Get-Date"),
            },
            _powershell_script, no_install="Install modules with Install-Module in $ shell mode.",
            search_dirs=lambda: [*_program_files("PowerShell", "7"),
                                 *((Path("/usr/local/microsoft/powershell/7"),) if IS_MAC else
                                   () if IS_WINDOWS else (Path("/opt/microsoft/powershell/7"),))],
        ),
        RuntimeInfo(
            Runtime.EXECUTABLE, (), "",
            {
                LaunchMode.SCRIPT: ModeInfo("Program file", "Program", "path/to/server (or .exe, .AppImage, .app)"),
                LaunchMode.COMMAND: _command_mode("e.g. ./caddy run, redis-server --port 6380"),
            },
            _program_script, no_install="Programs bring their own dependencies.",
        ),
    )
}


def runtime_info(runtime: Runtime) -> RuntimeInfo:
    return RUNTIMES[runtime]


def find_program(info: RuntimeInfo, explicit: str, env: Mapping[str, str]) -> str:
    """The runtime's executable: ``explicit`` (a path or a name), else the first one on PATH."""
    if not info.programs:
        return ""
    if explicit.strip():
        candidate = Path(explicit.strip().strip('"')).expanduser()
        if candidate.is_file():
            return str(candidate)
        if found := shutil.which(explicit.strip(), path=path_of(env)):
            return found
        raise LaunchError(f"{info.label} not found: {explicit}")
    search = os.pathsep.join(filter(None, [path_of(env) or "", *(str(d) for d in info.search_dirs())]))
    for name in info.programs:
        found = shutil.which(name, path=search)
        if found and not info.skip(found):
            return found
        if found:  # skipped (e.g. WSL's bash): look past it in the remaining directories
            rest = os.pathsep.join(d for d in search.split(os.pathsep) if Path(d) != Path(found).parent)
            if (other := shutil.which(name, path=rest)) and not info.skip(other):
                return other
    where = f" Install it from {info.website}" if info.website else ""
    raise LaunchError(f"{info.label} was not found on PATH.{where} or set its path in the service settings "
                      "(Environment → Interpreter).")
