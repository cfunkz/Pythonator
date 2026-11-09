"""Screen 2: the plan sheet, the Drift inbox and Try in a copy.
Every change is a plan. Lines that widen access await approval, bound to the risk hash you saw. Drift has its own
inbox (Heal, Adopt, Ignore) with what Pythonator put right by itself underneath. Try in a copy runs the command once
in a throwaway copy with traffic off and shows every file it changed and every address it tried."""

from __future__ import annotations

import difflib
import json
import time
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QBoxLayout, QDialog, QFrame, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget

from pythonator.ui import theme as th
from pythonator.ui.kit import Btn, Chip, Icon, InfoBtn, Panel, clear, esc, frame, hbox, label, lbl, primary, show_if, vbox

if TYPE_CHECKING:
    from pythonator.ui.app import Main

KIND_TONE = {"~": "muted", "!": "warn", "+": "ok", "-": "bad"}
DRIFT = {
    "blocked": ("bad", "warn", "blocked"), "unhealthy": ("warn", "pulse", "crashed and gave up"),
    "changed": ("warn", "history", "runs an older version"), "missing": ("warn", "box", "should run, doesn't"),
    "extra": ("warn", "box", "runs, nothing wants it"),
}
FIX = {
    "heal": ("Heal", "primary", "Make it match the spec again: clear the back-off and start it."),
    "start": ("Start", "primary", "Start it."), "restart": ("Restart", "primary", "Restart it on the saved version."),
    "adopt": ("Adopt: set stopped", "", "Make the spec match what is real: it stays stopped."),
    "ignore": ("Ignore", "ghost", "Hide this until the background service restarts."),
    "open": ("Open settings", "", "It can't run until you change something."),
}



def short_hash(h: str) -> str:
    return f"{h[:4]}…{h[-4:]}" if len(h) > 8 else h

# --- plan lines ---------------------------------------------------------------------------------------------------
class PlanLine(QFrame):
    """One line: a kind glyph (~ + − !), the change in mono, why on the right. Lines that widen are tinted."""

    def __init__(self, kind: str, text: str, why: str) -> None:
        super().__init__()
        self.kind = kind
        g = label({"-": "−"}.get(kind, kind), "Mono")
        g.setFixedWidth(14)
        g.setStyleSheet(f"color: {th.tone(KIND_TONE.get(kind, 'muted'))}; font-weight: 700;")
        t = label(text, "Mono", wrap=True)
        t.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        t.setMinimumWidth(80)
        w = label(why, "Small")
        w.setStyleSheet(f"color: {th.current().muted};")
        lay = hbox(g, spacing=12, margins=(22, 9, 22, 9))
        lay.addWidget(t, 1)
        lay.addWidget(w, 0, Qt.AlignmentFlag.AlignTop)
        self.setLayout(lay)
        self.retheme()

    def retheme(self) -> None:
        if self.kind == "!":
            t = th.current()
            self.setStyleSheet(f"PlanLine {{ background: {th.mix(t.text, t.raised, 0.05).name()}; }}")

def _group(text: str, ico: str) -> QWidget:
    return frame("", hbox(Icon(ico, "muted", 13), lbl(text), 1, spacing=7, margins=(22, 12, 22, 4)))

def plan_rows(res: dict[str, Any], new: bool = False, running: bool = False) -> list[QWidget]:
    """The plan's lines as widgets, grouped by who asked for them, then the Guard verdict and any refusal."""
    out: list[QWidget] = []
    lines = res.get("changes") or []
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    mine = []
    for x in lines:
        (by_plugin.setdefault(str(x["by"]), []) if x.get("by") else mine).append(x)
    if mine:
        out.append(_group("Requested by you, in New service" if new else "Requested by you, in settings", "user"))
        out += [PlanLine(str(x["kind"]), str(x["text"]), str(x["why"])) for x in mine]
    for who, xs in by_plugin.items():
        out.append(_group(f"Requested by plugin {who}", "plug"))
        out += [PlanLine(str(x["kind"]), str(x["text"]), str(x["why"])) for x in xs]
    if running and lines:
        out.append(PlanLine("~", "Restart (graceful, 5 s)", "needed for run"))
    if not lines:
        out.append(frame("", hbox(Icon("check", "ok", 14), label(
            f"Nothing is waiting: generation {res.get('generation', [0])[0]} is approved"
            + (" and running." if running else "."), "Muted"), 1, spacing=8, margins=(22, 14, 22, 14))))
    plan = res.get("plan") or {}
    if plan and new:
        out.append(_group(f"How it runs on this machine · {plan.get('os', '')}", "layers"))
        for x in plan.get("layers", []):
            if x["status"] != "off":
                tone = {"enforced": "+", "reduced": "~", "not-enforced": "!"}.get(x["status"], "~")
                out.append(PlanLine(tone, x["title"], f"{x['by']} · {x['status']}"))
    guard = guard_card(res)
    if guard is not None:
        out.append(guard)
    r = res.get("refused")
    if r:
        from pythonator.ui.kit import Note

        n = Note(f"<b>{esc(r['code'])}</b> {esc(r['message'])}" + (f"<br>Fix: {esc(r['fix'])}" if r.get("fix") else ""), "bad")
        out.append(frame("", vbox(n, margins=(22, 6, 22, 6))))
    return out

def guard_card(res: dict[str, Any]) -> QWidget | None:
    """`Guard: allowed (rules). Exposure 3.1 → 3.4.` in an accent-bordered box with an (i)."""
    g = res.get("guard")
    ex = res.get("exposure") or []
    if g is None and not ex:
        return None
    t = th.current()
    ok = all(x["allow"] for x in g or [])
    if g:
        x = next((y for y in g if not y["allow"]), g[0])
        verdict = (f"{esc(x['plugin'])}: <b>{'allowed' if ok else 'refused'}</b>"
                   + (f" ({esc(x['summary'])})" if ok and x.get("summary") else f": {esc(x['reason'])}" if not ok else ""))
    else:
        verdict = "No Guard installed: <b>your approval is the only check</b>"
    if len(ex) == 2:
        verdict += f". Exposure {ex[0]:.1f} → {ex[1]:.1f}." if ex[0] and ex[0] != ex[1] else f". Exposure {ex[1]:.1f}."
    info = InfoBtn("Guard", "Guard checks every change against your rules before Pythonator saves it. It can refuse, "
                   "never widen. Exposure is a 0–10 score of how far a service can reach if its code turns hostile.",
                   [("Mode", "Full access starts at 6"), ("Traffic", "any host 2, each allowed host 0.15"),
                    ("Ports", "0.3 local, 1 from the network")], foot="Rules live in Security → Guard rules", ico="shield")
    lab = label(verdict, rich=True, wrap=True)
    row = hbox(Icon("shield", "accent_text" if ok else "bad", 15), spacing=10, margins=(14, 11, 12, 11))
    row.addWidget(lab, 1)
    row.addWidget(info, 0, Qt.AlignmentFlag.AlignTop)
    box = frame("", row)
    c = t.accent if ok else t.bad
    box.setObjectName("GuardBox")
    box.setStyleSheet(f"#GuardBox {{ background: {th.mix(c, t.raised, 0.06).name()}; border: 1px solid "
                      f"{th.mix(c, t.raised, 0.45).name()}; border-radius: 6px; }}")
    return frame("", vbox(box, margins=(22, 12, 22, 14)))

# --- the plan sheet -----------------------------------------------------------------------------------------------
class PlanSheet(Panel):
    def __init__(self, page: PlanPage) -> None:
        self.page = page
        self.gen = Chip("gen 1 → 2", "line", "history")
        self.info = InfoBtn("Plan", "")
        self.need = Chip("", "warn", "warn")
        super().__init__("Plan", "list", self.info, [self.need], pad=0)
        head = self.layout().itemAt(0).widget().layout()  # type: ignore[union-attr]
        head.insertWidget(2, self.gen)
        head.setContentsMargins(20, 14, 18, 14)
        head.setSpacing(10)
        self.title.setObjectName("H2")
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.box.addLayout(self.rows)
        self.approve = primary("Approve and apply all", "check", page.approve_all)
        self.safe = Btn("Apply safe steps only", on=page.apply_safe)
        self.diff = Btn("Open diff", "file", "ghost", on=page.open_diff)
        self.foot = foot = frame("PanelFoot", hbox(self.approve, self.safe, self.diff, 1, spacing=8, margins=(18, 12, 18, 12)))
        t = th.current()
        foot.setStyleSheet(f"#PanelFoot {{ border-top: 1px solid {t.border}; }}")
        self.box.addWidget(foot)

    def set(self, res: dict[str, Any], running: bool) -> None:
        name = res.get("name") or res["spec"]["name"]
        lines = res.get("changes") or []
        steps = len(lines) + (1 if running and lines else 0)
        self.title.setText(f"Plan · {name} · {steps} step{'s' if steps != 1 else ''}" if steps else f"Plan · {name}")
        show_if(self.foot, bool(lines))
        g = res.get("generation") or [0, 0]
        self.gen.set(f"gen {g[0]} → {g[1]}" if g[0] != g[1] else f"gen {g[0]}")
        waiting = sum(1 for x in lines if x.get("widens"))
        self.need.set(f"{waiting} need approval")
        show_if(self.need, waiting > 0)
        self.info.set_card(
            "Every change is a plan. Lines that widen access await approval, which is bound to the risk hash below, so "
            "a plan that changes after you looked can't be approved with it.",
            [("Risk hash", short_hash(res.get("hash", ""))), ("Safe lines", "apply without asking"),
             ("Approval", "per change, not per service")])
        clear(self.rows)
        for w in plan_rows(res, running=running):
            self.rows.addWidget(w)
        refused = bool(res.get("refused"))
        guard_no = any(not x["allow"] for x in res.get("guard") or [])
        self.approve.setText(f"Approve {waiting} and apply all" if waiting else "Apply all")
        self.approve.setEnabled(bool(lines) and not refused and not guard_no)
        self.safe.setEnabled(bool(lines) and res.get("base") is not None and any(not x.get("widens") for x in lines))
        self.diff.setEnabled(res.get("base") is not None and bool(lines))

# --- drift inbox --------------------------------------------------------------------------------------------------
class EdgeCard(QFrame):
    """A raised card with a 3 px coloured left edge (drift: warn/bad, healed: ok)."""

    def __init__(self, tone: str, ico: str, title: str, detail: str, buttons: list[QWidget] = ()) -> None:  # type: ignore[assignment]
        super().__init__()
        self.tone = tone
        self.setObjectName("EdgeCard")
        head = label(title, "CardHead", rich=True)
        head.setWordWrap(True)
        body = label(detail, "Muted", wrap=True)
        lay = vbox(hbox(Icon(ico, tone, 15), head, 1, spacing=9), body, spacing=7, margins=(16, 13, 14, 14))
        if buttons:
            lay.addSpacing(4)
            lay.addLayout(hbox(*buttons, 1, spacing=6))
        self.setLayout(lay)
        self.retheme()

    def retheme(self) -> None:
        t = th.current()
        c = th.tone(self.tone)
        self.setStyleSheet(f"#EdgeCard {{ background: {t.raised}; border: 1px solid {t.border}; border-left: 3px solid {c};"
                           f" border-radius: 8px; }}")

class DriftInbox(QWidget):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.main = main
        self.setObjectName("SidePanel")
        self.count = Chip("0", "warn")
        info = InfoBtn("Drift", "Drift is when what runs no longer matches what the spec says. Pythonator heals what it "
                       "safely can by itself (listed under Healed today) and brings the rest here.",
                       [("Heal", "make it match the spec"), ("Adopt", "make the spec match reality"),
                        ("Ignore", "hide it until the next restart")], ico="history")
        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        self.healed = QVBoxLayout()
        self.healed.setSpacing(10)
        self.healed_lbl = lbl("Healed today")
        lay = vbox(hbox(label("Drift", "H2"), 1, self.count, info, spacing=8), 4, self.rows, 10, self.healed_lbl,
                   self.healed, 1, spacing=8, margins=(18, 18, 18, 18))
        self.setLayout(lay)

    def set(self, drift: list[dict[str, Any]], healed: list[dict[str, Any]]) -> None:
        clear(self.rows)
        clear(self.healed)
        self.count.set(str(len(drift)), "warn" if drift else "ok")
        if not drift:
            self.rows.addWidget(label("Nothing drifted. Every service matches its spec.", "Muted", wrap=True))
        for d in drift:
            tone, ico, title = DRIFT.get(d["kind"], ("warn", "warn", d["kind"]))
            btns: list[QWidget] = []
            for fx in d.get("fixes", ()):
                text, kind, tip = FIX.get(fx, (fx, "", ""))
                b = Btn(text, kind=kind, size="sm", tip=tip, on=lambda n=d["name"], k=d["kind"], f=fx: self.fix(n, k, f))
                btns.append(b)
            self.rows.addWidget(EdgeCard(tone, ico, f"{esc(d['name'])} · {esc(title)}", d.get("detail") or "", btns))
        show_if(self.healed_lbl, bool(healed))
        for h in reversed(healed[-8:]):
            when = time.strftime("%H:%M", time.localtime(h["ts"]))
            self.healed.addWidget(EdgeCard("ok", "check", f"{esc(h['name'])} · {esc(h['title'])}", f"{h['detail']} ({when})"))

    def fix(self, name: str, kind: str, action: str) -> None:
        if action == "open":
            self.main.open_settings(name)
            return
        self.main.link.call("drift.fix", self.main.after, name=name, kind=kind, action=action)

# --- try in a copy ------------------------------------------------------------------------------------------------
class TryResult(QWidget):
    """Two panels side by side: the files the copy changed (Keep / Discard) and the addresses it tried (Allow)."""

    def __init__(self, main: Main, spec: dict[str, Any], on_done: Any = None, on_allow: Any = None) -> None:
        super().__init__()
        self.main, self.spec, self.on_done, self.on_allow = main, spec, on_done, on_allow
        self.res: dict[str, Any] = {}
        entry = next((c for c in spec["command"] if "." in c and "/" not in c), spec["command"][0])
        tinfo = InfoBtn("Try in a copy", "Runs the command once in a throwaway copy of its folder (no .git, venvs or "
                        "caches, 200 MB max), in Sandbox mode with traffic off. You see every file it created, changed or "
                        "deleted and every address it tried, then keep the changes or throw the copy away.",
                        [("Shortcut", "Ctrl T"), ("Time limit", "60 s, then it is stopped")],
                        foot="Approval is per change, not per service", ico="play")
        self.files = Panel(f"Try in a copy · {entry}", "play", tinfo, [Chip("traffic off", "acc", "lock")], card=True)
        self.file_rows = QVBoxLayout()
        self.file_rows.setSpacing(6)
        self.files.box.addLayout(self.file_rows)
        self.keep = Btn("Keep changes", "check", "primary", "sm", on=self.keep_changes)
        self.discard = Btn("Discard copy", "x", size="sm", on=self.discard_copy)
        self.files.box.addLayout(hbox(self.keep, self.discard, 1, spacing=6))
        self.stats = label("", "MonoMuted")
        self.reach = Panel("It tried to reach", "globe", right=[self.stats], card=True)
        self.reach_rows = QVBoxLayout()
        self.reach_rows.setSpacing(6)
        self.reach.box.addLayout(self.reach_rows)
        self.allow = Btn("Allow", "check", size="sm", on=self.allow_hosts)
        self.reach.box.addLayout(hbox(self.allow, 1))
        self.out = QPlainTextEdit()
        self.out.setObjectName("Console")
        self.out.setReadOnly(True)
        self.out.setMaximumHeight(140)
        self.out.setFont(th.mono_font(12))
        self.out.hide()
        self.setLayout(vbox(hbox(self.files, self.reach, spacing=14), self.out, spacing=10))
        for b in (self.keep, self.discard, self.allow):
            b.setEnabled(False)
        self.file_rows.addWidget(label("Copying the folder and running it…", "Muted"))

    def run(self) -> None:
        self.main.link.call("try.run", self._got, timeout=60, **({"name": self.spec["name"]} if self.main._svc(
            self.spec["name"]) and not self.spec.get("_unsaved") else {"spec": {k: v for k, v in self.spec.items()
                                                                                if not k.startswith("_")}}))

    def _got(self, res: Any, err: Any) -> None:
        clear(self.file_rows)
        if err:
            self.file_rows.addWidget(label(err["message"] + (f"\nFix: {err['fix']}" if err.get("fix") else ""), "Muted", wrap=True))
            return
        self.res = res
        self.stats.setText(f"exit {res['exit'] if res['exit'] is not None else '–'} · {res['peak_mb']} MB · {res['seconds']} s"
                           + (" · stopped at the limit" if res["timed_out"] else ""))
        ico = {"added": ("plus", "ok"), "changed": ("sliders", "warn"), "deleted": ("trash", "bad")}
        for f in res["files"][:12]:
            i, tone = ico[f["change"]]
            self.file_rows.addWidget(frame("", hbox(Icon(i, tone, 15), label(f["path"], "Mono"), 1,
                                                    label(f["detail"], "MonoMuted"), spacing=10)))
        if len(res["files"]) > 12:
            self.file_rows.addWidget(label(f"… and {len(res['files']) - 12} more", "Small"))
        if not res["files"]:
            self.file_rows.addWidget(label("It didn't change any file.", "Muted"))
        clear(self.reach_rows)
        for r in res["reached"][:10]:
            why = r.get("why") or ""
            tag = ("metadata" if "metadata" in why else "this computer" if why == "this computer"
                   else "LAN" if "network" in why else r["verdict"])
            self.reach_rows.addWidget(frame("", hbox(Icon("x" if r["verdict"] != "allowed" else "check",
                                                          "bad" if r["verdict"] != "allowed" else "ok", 15),
                                                     label(f"{r['host']}:{r['port']}", "Mono"), 1,
                                                     Chip(tag, "bad" if r["verdict"] != "allowed" else "ok"), spacing=10)))
        if not res["reached"]:
            self.reach_rows.addWidget(label("It didn't try to reach anything.", "Muted"))
        self.wanted = sorted({r["host"] for r in res["reached"] if not r["host"].startswith("169.254.")
                              and not r["host"].replace(".", "").isdigit()})
        self.allow.setText(f"Allow {', '.join(self.wanted[:2])}{' …' if len(self.wanted) > 2 else ''} and run as a service")
        show_if(self.allow, bool(self.wanted))
        self.allow.setEnabled(bool(self.wanted))
        self.keep.setEnabled(bool(res["files"]))
        self.discard.setEnabled(True)
        if res.get("output"):
            self.out.setPlainText(res["output"][-4000:])
            self.out.show()

    def keep_changes(self) -> None:
        def got(r: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                return
            msg = f"kept {r['applied']} change{'s' if r['applied'] != 1 else ''}" + (
                f", {len(r['failed'])} refused: {r['failed'][0]}" if r["failed"] else "")
            self.main.toast("info" if not r["failed"] else "warn", self.spec["name"], msg)
            self._done()

        self.main.link.call("try.keep", got, id=self.res["id"])

    def discard_copy(self) -> None:
        self.main.link.call("try.discard", lambda _r, _e: self._done(), id=self.res["id"])

    def allow_hosts(self) -> None:
        if self.on_allow:
            self.on_allow(self.wanted)
        else:
            for h in self.wanted:
                self.main.link.call("allow_host", self.main.after_err, name=self.spec["name"], host=h)
            self.main.toast("info", self.spec["name"], f"allowed {', '.join(self.wanted)}; review the plan to apply")
        if self.res:
            self.main.link.call("try.discard", None, id=self.res["id"])
        self._done(discarded=True)

    def _done(self, discarded: bool = True) -> None:
        for b in (self.keep, self.discard, self.allow):
            b.setEnabled(False)
        if self.on_done:
            self.on_done()

class TryCopy(QDialog):
    """Try in a copy from the menu, Ctrl+T or New service: the same two panels in a sheet."""

    def __init__(self, main: Main, s: dict[str, Any] | None, on_allow: Any = None) -> None:
        super().__init__(main)
        self.setWindowTitle("Try in a copy")
        self.setObjectName("Sheet")
        self.resize(900, 420)
        if not s:
            self.setLayout(vbox(label("Pick a service first.", "Muted"), margins=(20, 20, 20, 20)))
            return
        self.result = TryResult(main, s["spec"], on_done=self.accept, on_allow=on_allow)
        self.setLayout(vbox(self.result, margins=(16, 16, 16, 16)))
        self.result.run()

# --- the page -----------------------------------------------------------------------------------------------------
class PlanPage(QWidget):
    """Takes the work and inspector panes: the plan (and a Try result) in the middle, the Drift inbox on the right."""

    def __init__(self, main: Main, s: dict[str, Any] | None, inbox: bool = False, try_now: bool = False) -> None:
        super().__init__()
        self.main, self.s, self.res = main, s, {}
        self.name = s["spec"]["name"] if s else ""
        self.back = Btn("Back", "back", "ghost", on=self.close_page)
        self.sheet = PlanSheet(self)
        self.try_btn = Btn("Try in a copy", "play", on=self.try_copy, tip="Ctrl+T")
        self.try_box = QVBoxLayout()
        center = QWidget()
        cl = vbox(hbox(self.back, 1, self.try_btn, spacing=8), self.sheet, self.try_box, 1, spacing=14, margins=(22, 14, 22, 22))
        center.setLayout(cl)
        self.inbox = DriftInbox(main)
        self.inbox.setFixedWidth(336)
        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body.setSpacing(0)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(center, 1)
        self.body.addWidget(self.inbox)
        inner = QWidget()
        inner.setLayout(self.body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        self.setLayout(vbox(scroll, spacing=0))
        show_if(self.sheet, bool(s))
        show_if(self.try_btn, bool(s))
        self.on_snapshot(main.snap)
        self.load()
        if try_now:
            self.try_copy()

    def layout_for(self, w: int) -> None:
        wide = w >= 1180
        self.body.setDirection(QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom)
        if wide:
            self.inbox.setFixedWidth(336)
        else:
            self.inbox.setMinimumWidth(0)
            self.inbox.setMaximumWidth(16777215)

    def load(self) -> None:
        if not self.name:
            return

        def got(res: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                return
            self.res = res
            s = self.main._svc(self.name)
            running = bool(s and s["observed"]["phase"] in ("running", "starting"))
            self.sheet.set(res, running)

        self.main.link.call("plan", got, name=self.name)

    def on_snapshot(self, snap: dict[str, Any]) -> None:
        self.inbox.set(snap.get("drift", []), snap.get("healed", []))
        if self.name and not self.main._svc(self.name):
            self.close_page()

    def approve_all(self) -> None:
        res = self.res
        s = self.main._svc(self.name)
        running = bool(s and s["observed"]["phase"] in ("running", "starting"))

        def done(_r: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                self.load()
                return
            if running:
                self.main.link.call("restart", self.main.after_err, name=self.name)
            self.main.toast("ok", self.name, f"approved {short_hash(res['hash'])} and applied")
            self.main.snapshot()
            self.load()

        self.main.link.call("approve_all", done, name=self.name, hash=res["hash"])

    def apply_safe(self) -> None:
        def done(r: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                return
            self.main.toast("info", self.name, f"applied the safe lines; {len(r['waiting'])} still await approval")
            self.main.snapshot()
            self.load()

        self.main.link.call("apply_safe", done, name=self.name)

    def open_diff(self) -> None:
        base, spec = self.res.get("base") or {}, self.res.get("spec") or {}
        for d in (base, spec):
            d.pop("generation", None)
        a = json.dumps(base, indent=2, sort_keys=True).splitlines()
        b = json.dumps(spec, indent=2, sort_keys=True).splitlines()
        t = th.current()
        html = []
        for line in difflib.unified_diff(a, b, "approved", "this plan", lineterm="", n=2):
            c = t.ok if line.startswith("+") else t.bad if line.startswith("-") else t.info if line.startswith("@") else t.muted
            html.append(f"<span style='color:{c}'>{esc(line)}</span>")
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Diff · {self.name}")
        dlg.resize(720, 520)
        view = QPlainTextEdit()
        view.setObjectName("Console")
        view.setReadOnly(True)
        view.appendHtml("<pre>" + "<br>".join(html or ["no differences"]) + "</pre>")
        dlg.setLayout(vbox(view, margins=(12, 12, 12, 12)))
        dlg.exec()

    def try_copy(self) -> None:
        s = self.main._svc(self.name)
        if not s:
            return
        clear(self.try_box)
        self.try_box.addWidget(TryResult(self.main, s["spec"], on_done=self.load))
        w = self.try_box.itemAt(0).widget()
        w.run()  # type: ignore[union-attr]

    def close_page(self) -> None:
        self.main.services_page.close_task(self)
