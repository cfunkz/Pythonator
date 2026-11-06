"""AI assistant: ask Claude, OpenAI, Gemini, DeepSeek or your own Ollama about one service at a time.

It sees the service's recent output and (with ``read_files``) its files, read-only. It can only *suggest* changes:
each one opens Pythonator's review window, and nothing is written unless you press Apply there. It can't run
commands, delete files or reach anything but the AI service you chose. See ``pythonator_ai/safety.py`` for the
rules, which aren't settings.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pythonator_ai import context, providers, safety

from pythonator.sdk import Plugin, PluginError, ServiceEvent, ServiceInfo, ui

MAX_FILES = 8
SHOWN_TURNS = 4
PANEL_BUDGET = 900_000  # bytes of panel JSON (the host takes 1 MB per message): a whole 64k-token answer fits
PIECE = 4_000  # an answer is shown in paragraphs of about this size: while it streams, only the last one changes
HISTORY_TURNS = 6
NO_FILE = "Add a file…"
QUICK_PICK = "Quick question…"
MODELS_FILE, CHOICE_FILE = "models.json", "model.json"
MODELS_FRESH = 24 * 3600  # a model list older than this is fetched again (Refresh fetches it at once)
MODELS_RETRY = 600  # after a failed fetch, wait this long before trying again by itself
QUICK = {
    "crash": ("Explain the crash", "The service crashed or stopped with an error. Explain what went wrong, from the "
              "logs, in plain words. Then suggest a fix, as edit blocks if changing a file fixes it."),
    "logs": ("Check the logs", "Look through the recent output for errors, warnings and anything unusual. Summarize "
             "what you find and what to do about each, most important first."),
    "fix": ("Suggest a fix", "Find the most likely bug or problem in this service and propose a fix as edit blocks. "
            "Explain the cause first, briefly."),
    "longrun": ("Long-run check", "This runs for a long time unattended. Check it for what breaks long-running "
                "programs: unhandled errors, reconnects, memory or file-handle growth, blocking calls, logging. List "
                "the real risks you see in this code, most serious first, with fixes."),
    "explain": ("Explain it", "Explain what this service does and how it starts, for someone new to it."),
}


@dataclass
class Turn:
    question: str
    label: str = ""  # the button that asked it, if any
    answer: str = ""
    status: str = "waiting"  # waiting, streaming, done, error, stopped
    notes: list[str] = field(default_factory=list)
    edits: dict[str, list[context.Edit]] = field(default_factory=dict)
    proposals: dict[str, str] = field(default_factory=dict)  # path -> status text
    proposal_ids: dict[str, str] = field(default_factory=dict)  # proposal id -> path
    sent: str = ""  # what was sent, in short


class Assistant(Plugin):
    def on_start(self) -> None:
        self.lock = threading.RLock()
        self.memory: dict[str, context.ServiceMemory] = {}
        self.chats: dict[str, list[Turn]] = {}
        self.attached: dict[str, list[str]] = {}
        self.file_lists: dict[str, tuple[float, list[str], str]] = {}  # id -> (when, paths, entry)
        self.selected = ""
        self.labels: dict[str, str] = {}
        self.call: providers.Call | None = None
        self.rate = safety.RateLimit()
        self.status = ""
        self.last_render = 0.0
        self.locked = (self.data_dir / "locked").exists()
        self.models: dict[str, dict[str, Any]] = self._load(MODELS_FILE)  # provider id -> when, base, models
        self.choice: dict[str, str] = {k: v for k, v in self._load(CHOICE_FILE).items() if isinstance(v, str)}
        self.model_setting = self._model_setting()
        self.fetching = False
        self.fetch_failed = 0.0
        self._pick_default()
        self._fetch_models()
        self.render()

    # -- events from Pythonator --

    def on_output(self, service: ServiceInfo, lines: list[str]) -> None:
        with self.lock:
            self.memory.setdefault(service.id, context.ServiceMemory()).add(lines)

    def on_event(self, event: ServiceEvent) -> None:
        if event.kind in ("crashed", "exited", "gave_up", "install_failed", "blocked"):
            with self.lock:
                mem = self.memory.setdefault(event.service.id, context.ServiceMemory())
                code = f" with exit code {event.exit_code}" if event.exit_code is not None else ""
                mem.last_exit = f"{event.kind}{code} at {time.strftime('%H:%M:%S')}" + (
                    f" ({event.note})" if event.note else "")
        self._pick_default()
        self.render()

    def on_settings(self) -> None:
        if self._model_setting() != self.model_setting:  # a model typed in the settings wins over the picker's
            self.model_setting = self._model_setting()
            self.choice.pop(self._provider().id, None)
            self._save(CHOICE_FILE, self.choice)
        self.fetch_failed = 0.0
        self._fetch_models()
        self.render()

    def on_tick(self) -> None:
        self._pick_default()
        self._fetch_models()
        self.render()

    def on_stop(self) -> None:
        self._cancel()

    # -- the panel's buttons --

    def on_action(self, action: str, value: str) -> None:
        if action == "service":
            sid = self.labels.get(value, "")
            if sid:
                self.selected = sid
        elif action == "ask":
            self._ask(value, "")
        elif action == "quick":
            key = value if value in QUICK else next((k for k, (text, _) in QUICK.items() if text == value), "")
            if key:
                self._ask(QUICK[key][1], QUICK[key][0])
        elif action == "model" and value.strip():
            self.choice[self._provider().id] = value.strip()[:200]
            self._save(CHOICE_FILE, self.choice)
            self.status = ""
        elif action in ("models", "test"):
            self._fetch_models(force=True)
        elif action == "stop":
            self._cancel()
        elif action == "new":
            with self.lock:
                if not self.call:
                    self.chats.pop(self.selected, None)
        elif action == "attach" and value != NO_FILE:
            files = self.attached.setdefault(self.selected, [])
            if value not in files and len(files) < MAX_FILES:
                files.append(value)
        elif action == "detach":
            self.attached.pop(self.selected, None)
        elif action == "files":
            self.file_lists.pop(self.selected, None)
            threading.Thread(target=self._files_for, args=(self.selected,), daemon=True).start()
        elif action == "propose":
            index, _, path = value.partition(":")
            threading.Thread(target=self._propose, args=(self.selected, int(index or -1), path), daemon=True).start()
        elif action == "kill":
            self.locked = True
            (self.data_dir / "locked").write_text("locked from the panel\n", encoding="utf-8")
            self._cancel()
            self.log("Kill switch: the assistant is locked; nothing is sent until you unlock it")
            self.notify("AI assistant locked: nothing is sent until you unlock it in its panel", "warn")
        elif action == "unlock":
            self.locked = False
            (self.data_dir / "locked").unlink(missing_ok=True)
            self.log("Unlocked")
        self.render(force=True)

    def on_proposal(self, proposal_id: str, status: str, note: str) -> None:
        with self.lock:
            for turns in self.chats.values():
                for turn in turns:
                    if proposal_id in turn.proposal_ids:
                        path = turn.proposal_ids[proposal_id]
                        turn.proposals[path] = {"applied": "Applied", "rejected": "You rejected it",
                                                "expired": "Not reviewed in time",
                                                "failed": "Not applied"}.get(status, status) + (
                            f": {note}" if note else "")
        self.render(force=True)

    # -- asking --

    def _blocked(self) -> str | None:
        if self.locked:
            return "The assistant is locked (kill switch). Press Unlock to use it again."
        if self.test_mode:
            return ("Test mode: questions aren't sent. In Plugins → AI assistant, grant the permissions and untick "
                    "test mode.")
        if "network" not in self.permissions:
            return "It needs the network permission to reach the AI service (Plugins → AI assistant)."
        if not self.selected or self.selected not in self.services:
            return "Add a service first: the assistant works on one service at a time."
        return None

    def _ask(self, question: str, label: str) -> None:
        question = question.strip()
        if not question:
            return
        with self.lock:
            if why := self._blocked():
                self.status = why
                return
            if self.call is not None:
                self.status = "Wait for the current answer, or press Stop."
                return
            if len(question) > safety.MAX_QUESTION:
                self.status = f"The question is too long (at most {safety.MAX_QUESTION} characters)."
                return
            if not label and safety.redact(question, self._secrets())[1]:
                self.status = ("Not sent: your question looks like it holds a secret (a key, token or password). "
                               "Remove it and ask again.")
                self.log("Blocked a question that held a secret")
                return
            limits = safety.Limits.of(self.settings)
            if why := self.rate.check(limits.per_hour):
                self.status = why
                return
            self.rate.take()
            turn = Turn(question, label)
            self.chats.setdefault(self.selected, []).append(turn)
            self.call = providers.Call()
            self.status = ""
            sid = self.selected
        threading.Thread(target=self._run, args=(sid, turn, self.call, limits), daemon=True,
                         name="ai-request").start()

    def _run(self, sid: str, turn: Turn, call: providers.Call, limits: safety.Limits) -> None:
        provider = providers.find(self.settings.get("provider"))
        try:
            request = self._request(sid, turn, provider, limits)
            if call.cancelled:
                raise providers.Cancelled
            turn.status = "streaming"
            self.render(force=True)
            self.log(f"Asked {provider.label} ({request.model}) about {self._name(sid)}: {turn.sent}")

            def piece(text: str) -> None:
                turn.answer += text  # shown as it arrives
                self.render()

            result = providers.stream(request, call, piece)
            turn.answer = result.text
            turn.notes += result.notes
            if result.output_tokens:
                turn.notes.append(f"{result.input_tokens:,} tokens in, {result.output_tokens:,} out")
            turn.edits = context.parse_edits(turn.answer)
            turn.status = "done"
        except providers.Cancelled:
            turn.status = "stopped"
        except (providers.AIError, PluginError) as exc:
            turn.status = "error"
            turn.notes.append(str(exc))
        except Exception as exc:  # never leave the panel waiting
            turn.status = "error"
            turn.notes.append(f"Unexpected error: {exc}")
            raise
        finally:
            with self.lock:
                if self.call is call:
                    self.call = None
            self.render(force=True)

    def _request(self, sid: str, turn: Turn, provider: providers.Provider,
                 limits: safety.Limits) -> providers.Request:
        service = self.services[sid]
        mem = self.memory.get(sid) or context.ServiceMemory()
        logs = mem.tail(safety.clamp(self.settings.get("log_lines"), 300, 0, context.LOG_LINES))
        file_list: list[str] = []
        files: list[tuple[str, str]] = []
        notes: list[str] = []
        if "read_files" in self.permissions and self.settings.get("include_files", True):
            file_list, entry = self._files_for(sid)
            for path in self._pick_files(sid, logs, file_list, entry):
                try:
                    got = self.read_file(sid, path)
                    files.append((got["path"], str(got["text"]) + ("\n… (cut at 256 KB)" if got["truncated"] else "")))
                except PluginError as exc:
                    notes.append(f"{path}: {exc}")
        bundle = context.build(self._describe(service, mem), logs, file_list, files, turn.question,
                               limits.context_kb * 1024, self._secrets())
        sent = [f"{bundle.bytes // 1024 or 1} KB", f"{len(logs)} log lines" if logs else "no logs"]
        if bundle.files:
            sent.append(", ".join(bundle.files))
        if bundle.redacted:
            sent.append(f"{bundle.redacted} secret{'s' if bundle.redacted != 1 else ''} removed")
        turn.sent = " · ".join(sent)
        turn.notes += notes
        history: list[dict[str, str]] = []
        for old in self.chats.get(sid, [])[:-1][-HISTORY_TURNS:]:
            if old.status == "done" and old.answer:
                history += [{"role": "user", "content": safety.redact(old.question, self._secrets())[0]},
                            {"role": "assistant", "content": old.answer[:6000]}]
        return providers.Request(provider, self._model(provider), self._base(provider), self._key(), context.SYSTEM,
                                 [*history, {"role": "user", "content": bundle.text}], limits.output_tokens,
                                 limits.timeout, str(self.settings.get("effort") or "medium"),
                                 bool(self.settings.get("allow_lan_http")))

    def _pick_files(self, sid: str, logs: list[str], file_list: list[str], entry: str) -> list[str]:
        known = set(file_list)
        picked: list[str] = []

        def add(path: str | None) -> None:
            if path and path in known and path not in picked and len(picked) < MAX_FILES:
                picked.append(path)

        for name in reversed(context.traceback_files(logs)[-3:]):
            add(context.relative_to_service(name, known))
        for path in self.attached.get(sid, []):
            add(path)
        add(entry)
        for name in context.COMMON_FILES:
            if len(picked) >= 5:
                break
            add(name)
        return picked

    def _files_for(self, sid: str) -> tuple[list[str], str]:
        cached = self.file_lists.get(sid)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1], cached[2]
        try:
            data = self.list_files(sid)
        except PluginError as exc:
            self.status = f"Can't list the files: {exc}"
            return [], ""
        paths = [str(f["path"]) for f in data.get("files", []) if isinstance(f, dict)]
        entry = str(data.get("entry") or "")
        self.file_lists[sid] = (time.monotonic(), paths, entry)
        self.render()
        return paths, entry

    def _describe(self, s: ServiceInfo, mem: context.ServiceMemory) -> str:
        lines = [f"Name: {s.name}", f"Runtime: {s.runtime}", f"Isolation: {s.mode}", f"State: {s.state}",
                 f"Network: {'on' if s.network else 'off'}"]
        if s.ports:
            lines.append(f"Ports: {', '.join(map(str, s.ports))} (exposed to: {s.expose})")
        if s.memory_limit_mb:
            lines.append(f"Memory limit: {s.memory_limit_mb} MB")
        if s.restarts:
            lines.append(f"Restarts in the last 10 minutes: {s.restarts}")
        if mem.last_exit:
            lines.append(f"Last ended: {mem.last_exit}")
        entry = self.file_lists.get(s.id, (0, [], ""))[2]
        if entry:
            lines.append(f"Entry file: {entry}")
        return "\n".join(lines)

    def _propose(self, sid: str, index: int, path: str) -> None:
        with self.lock:
            turns = self.chats.get(sid, [])
            turn = turns[index] if 0 <= index < len(turns) else None
            edits = turn.edits.get(path) if turn else None
        if turn is None or not edits:
            return
        if "propose_changes" not in self.permissions:
            turn.proposals[path] = "Needs the propose_changes permission (Plugins → AI assistant)"
        else:
            turn.proposals[path] = "Opening the review window…"
            self.render(force=True)
            try:
                summary = f"Suggested by the AI assistant using {providers.find(self.settings.get('provider')).label}"
                pid = self.propose_change(sid, path, [(e.old, e.new) for e in edits], summary)
                turn.proposal_ids[pid] = path
                turn.proposals[path] = "Waiting for your review in the review window"
            except PluginError as exc:
                turn.proposals[path] = f"Refused: {exc}"
        self.render(force=True)

    # -- the model picker --

    def _provider(self) -> providers.Provider:
        return providers.find(self.settings.get("provider"))

    def _model_setting(self) -> str:
        return str(self.settings.get("model") or "").strip()

    def _model(self, provider: providers.Provider) -> str:
        """The picker's choice for this service, else the model in the settings, else the service's default."""
        return self.choice.get(provider.id) or self._model_setting() or provider.default_model

    def _base(self, provider: providers.Provider) -> str:
        base = str(self.settings.get("base_url") or "").strip()
        return base if provider.self_hosted and base else provider.base_url

    def _known_models(self, provider: providers.Provider) -> list[str]:
        """The cached list for this service (and this server, for a self-hosted one); [] if there's none."""
        entry = self.models.get(provider.id) or {}
        if entry.get("base") != self._base(provider) or not isinstance(entry.get("models"), list):
            return []
        return [str(m) for m in entry["models"]]

    def _fetch_models(self, force: bool = False) -> None:
        """Fetch the service's model list in the background when it's missing or a day old (or now, on Refresh)."""
        provider = self._provider()
        with self.lock:
            entry = self.models.get(provider.id) or {}
            age = time.time() - float(entry.get("when", 0))
            fresh = entry.get("base") == self._base(provider) and 0 <= age < MODELS_FRESH
            retrying = bool(self.fetch_failed) and time.monotonic() - self.fetch_failed < MODELS_RETRY
            why = self._cant_fetch(provider)
            if force and why:
                self.status = why
            if self.fetching or why or (not force and (fresh or retrying)):
                return
            self.fetching = True
            if force:
                self.status = f"Fetching {provider.label}'s models…"
        threading.Thread(target=self._fetch, args=(provider, force), daemon=True, name="ai-models").start()

    def _cant_fetch(self, provider: providers.Provider) -> str:
        if self.locked:
            return "The assistant is locked (kill switch). Press Unlock to use it again."
        if self.test_mode or "network" not in self.permissions:
            return "It needs the network permission to reach the AI service (Plugins → AI assistant)."
        if provider.needs_key and not self._key():
            return f"Add your {provider.label} API key first (Plugins → AI assistant → Settings)."
        return ""

    def _fetch(self, provider: providers.Provider, force: bool) -> None:
        base = self._base(provider)
        try:
            names = providers.chat_models(provider, providers.list_models(
                provider, base, self._key(), providers.Call(), 20, bool(self.settings.get("allow_lan_http"))))
            with self.lock:
                self.models[provider.id] = {"when": time.time(), "base": base, "models": names[:300]}
                self._save(MODELS_FILE, self.models)
                self.fetch_failed = 0.0
                if force:
                    self.status = f"Connected to {provider.label}: {len(names)} models to choose from."
        except providers.AIError as exc:
            with self.lock:
                self.fetch_failed = time.monotonic()
                if force:
                    self.status = f"Couldn't fetch the models: {exc}"
        finally:
            with self.lock:
                self.fetching = False
            self.render(force=True)

    def _load(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads((self.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, name: str, data: dict[str, Any]) -> None:
        try:
            (self.data_dir / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as exc:
            self.log(f"Couldn't save {name}: {exc}")

    def _cancel(self) -> None:
        with self.lock:
            call = self.call
        if call is not None:
            call.cancel()

    def _key(self) -> str:
        return str(self.settings.get("api_key") or "").strip()

    def _secrets(self) -> tuple[str, ...]:
        return (self._key(),) if self._key() else ()

    def _name(self, sid: str) -> str:
        return self.services[sid].name if sid in self.services else sid

    def _pick_default(self) -> None:
        if self.selected not in self.services and self.services:
            self.selected = sorted(self.services.values(), key=lambda s: s.name.lower())[0].id

    # -- drawing --

    def render(self, force: bool = False) -> None:
        if "ui" not in self.permissions:
            return
        now = time.monotonic()
        if not force and now - self.last_render < 0.4:
            return
        self.last_render = now
        with self.lock:
            panel = self._panel()
        self.show(panel)

    def _panel(self) -> dict[str, Any]:
        provider = self._provider()
        model = self._model(provider)
        limits = safety.Limits.of(self.settings)
        services = sorted(self.services.values(), key=lambda s: s.name.lower())
        self.labels = {}
        for s in services:
            label = s.name if s.name not in self.labels else f"{s.name} ({s.id[:6]})"
            self.labels[label] = s.id
        current = next((k for k, v in self.labels.items() if v == self.selected), "")
        known = self._known_models(provider)
        options = list(dict.fromkeys([model, *(known or provider.models)]))[:300] if model else known[:300]
        head: list[dict[str, Any]] = [
            ui.row(ui.select("service", list(self.labels), current, "Service")),
            ui.row(ui.select("model", options, model, "Model"),
                   ui.button("Fetching…" if self.fetching else "↻ Refresh", "models")),
            ui.text(f"{provider.label} · {self.rate.used()} of {limits.per_hour} questions this hour", "muted"),
        ]
        if why := self._blocked():
            head.append(ui.text(why, "warn"))
        elif provider.needs_key and not self._key():
            head.append(ui.text(f"Add your {provider.label} API key in Plugins → AI assistant → Settings. It is kept "
                                "in your system keychain.", "warn"))
        if known and model not in known:
            head.append(ui.text(f"{model} isn't one of {provider.label}'s models: pick one from the list.", "warn"))
        if self.status:
            head.append(ui.text(self.status, "error" if self.status.startswith(
                ("Not sent", "Couldn't", "Can't")) else ""))
        foot = self._foot()
        turns = self.chats.get(self.selected, [])
        start = max(0, len(turns) - SHOWN_TURNS)
        while True:
            messages: list[dict[str, Any]] = []
            reviews: list[dict[str, Any]] = []
            if start:
                messages.append(ui.message("note", f"{start} earlier question{'s' if start > 1 else ''} not shown"))
            if not turns and self.selected:
                messages.append(ui.message("note", f"Ask anything about {self._name(self.selected)}: why it stopped, "
                                           "what the logs say, how to fix something. Or pick a quick question "
                                           "below."))
            for i in range(start, len(turns)):
                messages += self._turn(turns[i])
                reviews += self._reviews(i, turns[i])
            if len(messages) > 200 and start < len(turns) - 1:  # too many pieces: show fewer earlier questions
                start += 1
                continue
            panel = ui.panel("AI assistant", *head, ui.chat(*messages[-200:]), *reviews, *foot)
            size = len(json.dumps(panel, ensure_ascii=False).encode())
            if size <= PANEL_BUDGET:
                return panel
            if start < len(turns) - 1:
                start += 1
                continue
            # One answer bigger than a panel can carry (only near the hard output cap, in a script of 3-byte
            # characters): its beginning goes, never the part that is still arriving.
            shown = messages[-200:]
            while len(shown) > 1 and size > PANEL_BUDGET:
                size -= len(json.dumps(shown.pop(0), ensure_ascii=False).encode())
            note = ui.message("note", "The start of this answer is longer than the panel holds and isn't shown.")
            return ui.panel("AI assistant", *head, ui.chat(note, *shown), *reviews, *foot)

    def _foot(self) -> list[dict[str, Any]]:
        name = self._name(self.selected) if self.selected else ""
        foot = [ui.input("ask", f"Ask about {name}… (Enter sends, Shift+Enter: new line)" if name else "Ask…", "Ask")]
        tools = [ui.select("quick", [QUICK_PICK, *(text for text, _ in QUICK.values())], QUICK_PICK)]
        attached = self.attached.get(self.selected, [])
        if "read_files" in self.permissions and self.selected:
            paths = self.file_lists.get(self.selected, (0, [], ""))[1]
            tools.append(ui.select("attach", [NO_FILE, *paths[:299]], NO_FILE) if paths
                         else ui.button("Add files…", "files"))
        foot.append(ui.row(*tools))
        if attached:
            foot.append(ui.row(ui.text("Also sent: " + ", ".join(attached), "muted"), ui.button("Clear", "detach")))
        elif self.selected and "read_files" not in self.permissions:
            foot.append(ui.text("Only the logs are sent. Grant read_files to include the service's files.", "muted"))
        foot += [
            ui.row(*([ui.button("■ Stop", "stop")] if self.call else []), ui.button("New chat", "new"),
                   ui.button("Unlock", "unlock") if self.locked else ui.button("⛔ Kill switch", "kill")),
            ui.text("It only answers: no commands, no writing. A suggested change opens a review window and only "
                    "your Apply writes it. Secrets are removed before anything is sent.", "muted"),
        ]
        return foot

    def _turn(self, turn: Turn) -> list[dict[str, Any]]:
        items = [ui.message("you", (turn.label or turn.question)[:4000])]
        if turn.sent:
            items.append(ui.message("note", f"Sent {turn.sent}"))
        answer = turn.answer
        if turn.status == "waiting":
            items.append(ui.message("note", "Gathering the logs and files…"))
        elif turn.status == "streaming" and not answer:
            items.append(ui.message("ai", "Thinking…"))
        for kind, text in context.split_answer(answer):
            if kind == "text" and not text.strip():
                continue
            if kind == "text":
                for piece in _paragraphs(text.strip("\n"), PIECE):
                    items.append(ui.message("ai", piece, markdown=True))
            else:
                for chunk in _chunks(text, 19_000):
                    items.append(ui.message("ai", chunk, code=True))
        if turn.status == "stopped":
            items.append(ui.message("note", "Stopped."))
        for note in turn.notes:
            items.append(ui.message("error" if turn.status == "error" else "note", note[:2000]))
        for path, state in turn.proposals.items():
            items.append(ui.message("note", f"{path}: {state}"))
        return items

    def _reviews(self, index: int, turn: Turn) -> list[dict[str, Any]]:
        """A Review button for each file the answer would change, until its change is applied or waiting."""
        out = []
        for path, edits in turn.edits.items():
            state = turn.proposals.get(path, "")
            if not state or state.startswith(("Refused", "Not applied", "You rejected", "Not reviewed")):
                out.append(ui.row(ui.button(f"Review change to {path} ({len(edits)} edit"
                                            f"{'s' if len(edits) > 1 else ''})", "propose", f"{index}:{path}")))
        return out


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _paragraphs(text: str, size: int) -> list[str]:
    """``text`` in pieces of about ``size`` characters, cut only between paragraphs (a blank line), so each
    piece is whole Markdown; a single paragraph longer than 19,000 characters is cut where it must be."""
    pieces: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if current and len(current) + len(para) + 2 > size:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current or not pieces:
        pieces.append(current)
    return [chunk for piece in pieces for chunk in _chunks(piece, 19_000)]
