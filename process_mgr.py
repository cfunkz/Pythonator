"""Process Manager - Handles bot process lifecycle with isolation."""
from __future__ import annotations
import codecs, os, shlex, shutil, signal, subprocess, sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol
from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer
from config import APP_DIR, Bot, KILL_TIMEOUT_MS

class OutputCallback(Protocol):
    def __call__(self, name: str, text: str) -> None: ...

class FinishedCallback(Protocol):
    def __call__(self, name: str, exit_code: int, crashed: bool) -> None: ...

@dataclass
class ProcessState:
    process: QProcess
    stdout_dec: codecs.IncrementalDecoder = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    stderr_dec: codecs.IncrementalDecoder = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    use_pgroup: bool = False
    stopping: bool = False

class ProcessManager(QObject):
    def __init__(self, on_output: OutputCallback, on_finished: FinishedCallback):
        super().__init__()
        self._on_output, self._on_finished = on_output, on_finished
        self._procs: dict[str, ProcessState] = {}

    @property
    def running(self) -> set[str]: return set(self._procs.keys())
    def is_running(self, name: str) -> bool: return name in self._procs
    def get_pid(self, name: str) -> int:
        s = self._procs.get(name); return s.process.processId() or 0 if s else 0

    def start(self, bot: Bot) -> bool:
        if bot.name in self._procs: self._log(bot.name, "[runner] Already running", "31"); return False
        return self._start_custom(bot) if bot.custom_cmd else self._start_script(bot)

    def stop(self, name: str) -> None:
        if not (state := self._procs.get(name)): return
        state.stopping = True
        pid = state.process.processId() or 0
        if os.name != "nt" and pid and state.use_pgroup:
            try: os.killpg(pid, signal.SIGTERM)
            except: pass
        state.process.terminate()
        QTimer.singleShot(KILL_TIMEOUT_MS, lambda: self._force_kill(name, pid))

    def stop_all(self) -> None:
        for name in list(self._procs.keys()): self.stop(name)

    def _start_script(self, bot: Bot) -> bool:
        if not bot.entry: self._log(bot.name, "[runner] No entry specified", "31"); return False
        entry = Path(bot.entry)
        if not entry.exists(): self._log(bot.name, "[runner] Entry not found", "31"); return False
        python = self._resolve_python(bot, entry)
        if not python: return False
        args = ["-u", str(entry)]
        if bot.flags:
            try: args.extend(shlex.split(bot.flags))
            except: args.extend(bot.flags.split())
        return self._run(bot.name, python, args, entry.parent, self._get_venv(entry))

    def _start_custom(self, bot: Bot) -> bool:
        if not bot.flags.strip(): self._log(bot.name, "[runner] No command specified", "31"); return False
        cwd = Path(bot.entry).parent if bot.entry and Path(bot.entry).exists() else APP_DIR
        python = self._resolve_python(bot, cwd / "main.py")
        if not python: return False
        try: parts = shlex.split(bot.flags)
        except ValueError as e: self._log(bot.name, f"[runner] Invalid command: {e}", "31"); return False
        args = parts[1:] if parts and parts[0] in ("python", "python3", Path(python).name) else ["-m"] + parts
        return self._run(bot.name, python, args, cwd, self._get_venv(cwd / "main.py"))

    def _resolve_python(self, bot: Bot, entry: Path) -> Optional[str]:
        if bot.python_path:
            if Path(bot.python_path).exists(): return bot.python_path
            self._log(bot.name, f"[runner] Python not found: {bot.python_path}", "31"); return None
        venv = self._get_venv(entry)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.exists(): return str(python)
        self._log(bot.name, "[runner] No venv - run Setup venv or set Python path", "31"); return None

    def _get_venv(self, entry: Path) -> Path: return entry.parent / ".venv"

    def _run(self, name: str, program: str, args: list[str], cwd: Path, venv: Optional[Path] = None) -> bool:
        proc = QProcess(self)
        use_pgroup = False
        if os.name != "nt" and shutil.which("setsid"):
            cmd = " ".join(shlex.quote(x) for x in [program] + args)
            proc.setProgram("bash"); proc.setArguments(["-lc", f"exec setsid {cmd}"])
            use_pgroup = True
        else:
            proc.setProgram(program); proc.setArguments(args)
        proc.setWorkingDirectory(str(cwd))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in [("PYTHONUNBUFFERED", "1"), ("PYTHONUTF8", "1"), ("PYTHONIOENCODING", "utf-8"),
                     ("TERM", "xterm-256color"), ("FORCE_COLOR", "1")]: env.insert(k, v)
        env.remove("PYTHONHOME")
        if venv and venv.exists():
            env.insert("VIRTUAL_ENV", str(venv))
            env.insert("PATH", str(venv / ("Scripts" if os.name == "nt" else "bin")) + os.pathsep + env.value("PATH", ""))
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(lambda n=name: self._on_stdout(n))
        proc.readyReadStandardError.connect(lambda n=name: self._on_stderr(n))
        proc.finished.connect(lambda code, status, n=name: self._handle_finished(n, code, status))
        self._procs[name] = ProcessState(process=proc, use_pgroup=use_pgroup)
        proc.start()
        self._log(name, f"[runner] Started {datetime.now():%H:%M:%S}", "36")
        return True

    def _on_stdout(self, name: str) -> None:
        if not (s := self._procs.get(name)): return
        if data := bytes(s.process.readAllStandardOutput().data()):
            if text := s.stdout_dec.decode(data): self._on_output(name, text)

    def _on_stderr(self, name: str) -> None:
        if not (s := self._procs.get(name)): return
        if data := bytes(s.process.readAllStandardError().data()):
            if text := s.stderr_dec.decode(data): self._on_output(name, text)

    def _handle_finished(self, name: str, code: int, status: QProcess.ExitStatus) -> None:
        if not (state := self._procs.get(name)): return
        self._on_stdout(name); self._on_stderr(name)
        user_stop, crashed = state.stopping, status == QProcess.ExitStatus.CrashExit
        del self._procs[name]
        if user_stop: self._log(name, "[runner] Stopped", "36")
        else: self._log(name, f"[runner] {'CRASHED' if crashed else 'Exited'} (code={code})", "31" if crashed else "33")
        self._on_finished(name, code, crashed and not user_stop)

    def _force_kill(self, name: str, pid: int) -> None:
        if not (s := self._procs.get(name)) or s.process.state() == QProcess.ProcessState.NotRunning:
            return

        if os.name == "nt":
            if pid:
                try:
                    # Non-blocking: do NOT subprocess.run() on the UI thread
                    QProcess.startDetached(
                        "taskkill",
                        ["/PID", str(pid), "/T", "/F"],
                    )
                except Exception:
                    pass

            # Still do Qt kill as a fallback
            s.process.kill()
            return
        
        if pid and s.use_pgroup:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                s.process.kill()
        else:
            s.process.kill()

    def _log(self, name: str, msg: str, color: str = "0") -> None:
        self._on_output(name, f"\x1b[{color}m{msg}\x1b[0m\n")

    def setup_venv(self, bot: Bot) -> bool:
        entry = Path(bot.entry) if bot.entry and Path(bot.entry).exists() else None
        if not entry: return False
        venv = self._get_venv(entry)
        if (venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).exists():
            self._log(bot.name, "[runner] venv already exists", "33"); return False
        creator = bot.python_path.strip() or sys.executable
        if not creator or not Path(creator).exists():
            self._log(bot.name, f"[runner] Python not found: {creator}", "31"); return False
        self._log(bot.name, "[runner] Creating venv...", "36")
        return self._run(bot.name, creator, ["-m", "venv", str(venv)], entry.parent)

    def install_deps(self, bot: Bot) -> bool:
        entry = Path(bot.entry) if bot.entry and Path(bot.entry).exists() else None
        if not entry: return False
        python = self._resolve_python(bot, entry)
        if not python: return False
        (entry.parent / "requirements.txt").write_text(bot.reqs or "", encoding="utf-8")
        self._log(bot.name, "[runner] Installing dependencies...", "36")
        return self._run(bot.name, python, ["-m", "pip", "install", "-r", "requirements.txt"], entry.parent, self._get_venv(entry))
