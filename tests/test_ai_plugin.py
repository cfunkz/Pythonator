"""The AI assistant plugin (plugins/pythonator-ai): its safety rules, how it reads answers, and the two APIs it
speaks, against a fake AI server on this computer. No real AI service is called."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "pythonator-ai"
sys.path.insert(0, str(PLUGIN))

from pythonator_ai import context, providers, safety  # noqa: E402

# -- secrets never leave ---------------------------------------------------------------------------------------

FAKE_KEYS = [
    "sk-ant-api03-" + "a" * 40,
    "sk-proj-" + "B1" * 20,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "ghp_" + "x" * 36,
    "xoxb-1234567890-abcdefghij",
    "AIza" + "S" * 35,
    "123456789:AA" + "h" * 33,
    "sk_live_" + "4" * 24,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "https://discord.com/api/webhooks/123/abcDEF",
    "https://hooks.slack.com/services/T000/B000/XXXX",
]


@pytest.mark.parametrize("secret", FAKE_KEYS)
def test_known_secret_formats_are_removed(secret: str) -> None:
    text, count = safety.redact(f"starting with {secret} now")
    assert secret not in text and count >= 1
    assert safety.REDACTED in text


def test_private_keys_passwords_and_url_credentials_are_removed() -> None:
    sample = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
              'DB_PASSWORD = "hunter22hunter"\napi_key: abcdefgh12345678\n'
              "postgres://admin:s3cretpass@db.local:5432/app\nAuthorization: Bearer abcdefghijklmnop123\n")
    text, count = safety.redact(sample)
    for leaked in ("b3BlbnNzaC1rZXktdjEAAAA", "hunter22hunter", "abcdefgh12345678", "s3cretpass",
                   "abcdefghijklmnop123"):
        assert leaked not in text
    assert "postgres://[secret removed]@db.local" in text
    assert count >= 5


def test_code_that_merely_names_secrets_is_kept() -> None:
    code = 'token = get_token()\npassword = os.environ["PASSWORD"]\nif api_key is None:\n'
    text, count = safety.redact(code)
    assert text == code and count == 0


def test_the_key_in_use_is_removed_wherever_it_appears() -> None:
    text, count = safety.redact("printed my key: weird-custom-key-42 oops", ("weird-custom-key-42",))
    assert "weird-custom-key-42" not in text and count == 1


# -- only the AI service's address ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://api.anthropic.com/v1", "http://localhost:11434/v1",
                                 "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1", "https://my-llm.example.com/v1"])
def test_allowed_endpoints(url: str) -> None:
    assert safety.check_endpoint(url) is None


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",  # cloud metadata
    "https://169.254.169.254/",
    "http://[fe80::1]/",
    "http://192.168.1.20:11434/v1",  # LAN over plain http: off unless allowed
    "http://example.com/v1",  # plain http to the internet
    "https://user:pass@example.com/v1",
    "https://example.com/v1?x=1",
    "ftp://example.com/",
    "file:///etc/passwd",
    "http://0.0.0.0:80/",
])
def test_blocked_endpoints(url: str) -> None:
    assert safety.check_endpoint(url) is not None


def test_lan_http_only_when_allowed() -> None:
    assert safety.check_endpoint("http://192.168.1.20:11434/v1", allow_lan_http=True) is None
    assert safety.check_endpoint("http://8.8.8.8/v1", allow_lan_http=True) is not None


def test_limits_have_hard_caps() -> None:
    limits = safety.Limits.of({"requests_per_hour": 100000, "max_context_kb": 99999, "max_output_tokens": 10 ** 9,
                               "timeout": 99999})
    assert (limits.per_hour, limits.context_kb, limits.output_tokens, limits.timeout) == (
        safety.HARD_MAX_PER_HOUR, safety.HARD_MAX_CONTEXT_KB, safety.HARD_MAX_OUTPUT_TOKENS, safety.HARD_MAX_TIMEOUT)
    assert safety.Limits.of({"requests_per_hour": "nonsense"}).per_hour == 60


def test_rate_limit() -> None:
    rate = safety.RateLimit()
    assert rate.check(2, now=100.0) is None
    rate.take(100.0)
    assert "moment" in (rate.check(2, now=100.5) or "")
    rate.take(110.0)
    assert "Hourly limit" in (rate.check(2, now=120.0) or "")
    assert rate.check(2, now=3800.0) is None  # an hour later


# -- reading answers and building the prompt -----------------------------------------------------------------

ANSWER = """The port is wrong.

<<<<<<< EDIT app.py
PORT = 80
=======
PORT = int(os.environ.get("PORT", 8000))
>>>>>>> END

```bash
pip install flask
```

<<<<<<< EDIT ../../etc/passwd
x
=======
y
>>>>>>> END
<<<<<<< EDIT notes/NEW.md
=======
hello
>>>>>>> END
"""


def test_edit_blocks_are_parsed_and_unsafe_paths_dropped() -> None:
    edits = context.parse_edits(ANSWER)
    assert list(edits) == ["app.py", "notes/NEW.md"]
    assert edits["app.py"][0].old == "PORT = 80\n"
    assert edits["app.py"][0].new.startswith("PORT = int(")
    assert edits["notes/NEW.md"][0].old == ""
    kinds = [k for k, _ in context.split_answer(ANSWER)]
    assert kinds[:3] == ["text", "edit", "code"]


def test_prompt_marks_service_data_and_keeps_the_budget() -> None:
    logs = [f"line {i} token=abcdefgh{i:08d}" for i in range(5000)]
    files = [("app.py", "x = 1\n" * 50_000), ("small.py", "print('hi')\n")]
    bundle = context.build("Name: api", logs, ["app.py", "small.py"], files, "why?", 40_000, ())
    assert bundle.text.startswith("<service_data>")
    assert "My question:\nwhy?" in bundle.text
    assert len(bundle.text) < 46_000
    assert "abcdefgh00004999" not in bundle.text and bundle.redacted > 0
    assert "line 4999" in bundle.text, "the newest lines are kept"


def test_traceback_files_are_found() -> None:
    lines = ['Traceback (most recent call last):', '  File "/home/u/proj/src/app.py", line 3, in <module>',
             '  File "/usr/lib/python3.12/json/__init__.py", line 9', "ValueError: x"]
    names = context.traceback_files(lines)
    assert context.relative_to_service(names[0], {"src/app.py", "main.py"}) == "src/app.py"
    assert context.relative_to_service(names[1], {"src/app.py"}) is None


# -- the APIs, against a fake server ---------------------------------------------------------------------------


class FakeAI(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []  # noqa: RUF012 (one fake server per test, reset by the fixture)
    mode = "ok"

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._json(200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAI.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if FakeAI.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()
            return
        if FakeAI.mode == "401":
            self._json(401, {"error": {"message": "invalid x-api-key"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path.endswith("/messages"):
            events = [("message_start", {"type": "message_start", "message": {"model": "claude-x",
                                                                             "usage": {"input_tokens": 12}}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": "Hello "}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": "there"}}),
                      ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                         "usage": {"output_tokens": 5}})]
            for name, data in events:
                self._chunk(f"event: {name}\ndata: {json.dumps(data)}\n\n")
                if FakeAI.mode == "slow":
                    time.sleep(1)
        elif FakeAI.mode == "edit":
            answer = ("The port is taken.\n\n<<<<<<< EDIT app.py\nPORT = 80\n=======\nPORT = 8080\n>>>>>>> END\n")
            self._chunk(f"data: {json.dumps({'choices': [{'delta': {'content': answer}}]})}\n\n")
            self._chunk("data: [DONE]\n\n")
        else:
            for piece in ("Hi", " from", " OpenAI"):
                self._chunk(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n")
                if FakeAI.mode == "slow":
                    time.sleep(1)
            self._chunk(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n")
            self._chunk("data: [DONE]\n\n")

    def _chunk(self, text: str) -> None:
        try:
            self.wfile.write(text.encode())
            self.wfile.flush()
        except OSError:
            pass

    def _json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake() -> Iterator[str]:
    FakeAI.seen, FakeAI.mode = [], "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAI)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _req(provider_id: str, base: str, model: str = "") -> providers.Request:
    p = providers.find(provider_id)
    return providers.Request(p, model or p.default_model or "m", base, "test-key", "system text",
                             [{"role": "user", "content": "hi"}], 1000, 20)


def test_claude_streams_with_current_api(fake: str) -> None:
    pieces: list[str] = []
    result = providers.stream(_req("claude", fake), providers.Call(), pieces.append)
    assert result.text == "Hello there" and pieces == ["Hello ", "there"]
    assert (result.input_tokens, result.output_tokens, result.stop) == (12, 5, "end_turn")
    sent = FakeAI.seen[0]
    assert sent["path"] == "/v1/messages"
    assert sent["headers"]["x-api-key"] == "test-key"
    assert sent["headers"]["anthropic-version"] == "2026-10-21"
    body = sent["body"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["system"] == "system text"
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert "temperature" not in body


@pytest.mark.parametrize("pid", ["openai", "gemini", "deepseek", "ollama", "custom"])
def test_openai_compatible_services_stream(fake: str, pid: str) -> None:
    result = providers.stream(_req(pid, fake), providers.Call(), lambda _t: None)
    assert result.text == "Hi from OpenAI" and result.stop == "stop"
    body = FakeAI.seen[0]["body"]
    assert FakeAI.seen[0]["path"] == "/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "system text"}
    assert ("max_completion_tokens" if pid == "openai" else "max_tokens") in body


def test_errors_are_explained(fake: str) -> None:
    FakeAI.mode = "401"
    with pytest.raises(providers.AIError, match="invalid x-api-key"):
        providers.stream(_req("claude", fake), providers.Call(), lambda _t: None)


def test_redirects_are_not_followed(fake: str) -> None:
    FakeAI.mode = "redirect"
    with pytest.raises(providers.AIError, match="redirect"):
        providers.stream(_req("openai", fake), providers.Call(), lambda _t: None)


def test_stop_cancels_a_running_answer(fake: str) -> None:
    FakeAI.mode = "slow"
    call = providers.Call()
    threading.Timer(0.5, call.cancel).start()
    started = time.monotonic()
    with pytest.raises(providers.Cancelled):
        providers.stream(_req("ollama", fake), call, lambda _t: None)
    assert time.monotonic() - started < 2.5


def test_a_key_is_needed_for_cloud_services(fake: str) -> None:
    req = _req("claude", fake)
    req.api_key = ""
    with pytest.raises(providers.AIError, match="API key"):
        providers.stream(req, providers.Call(), lambda _t: None)


def test_connection_test_lists_models(fake: str) -> None:
    assert providers.list_models(providers.find("ollama"), fake, "", providers.Call(), 5, False) == ["model-a",
                                                                                                    "model-b"]


def test_metadata_address_is_refused_before_connecting() -> None:
    req = _req("custom", "http://169.254.169.254/v1")
    with pytest.raises(providers.AIError, match="Blocked"):
        providers.stream(req, providers.Call(), lambda _t: None)


# -- the whole bundled plugin, in the real background service -------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps the plugin's AppContainer off this PC's own "
                    "servers, so it can't reach the fake AI server on 127.0.0.1 (real AI services are remote)")
def test_bundled_plugin_answers_and_its_fix_is_applied_after_review(harness: Any, project: Path, fake: str) -> None:
    from tests.test_plugin_files import _activity

    FakeAI.mode = "edit"
    harness.daemon.plugins.vault.use_keychain = False
    (project / "app.py").write_text("import time\nPORT = 80\nprint('serving', PORT, 'token=abcdefghij123456')\n"
                                    "time.sleep(60)\n", encoding="utf-8")
    sid = harness.add(name="web", folder=str(project), runtime="python", entry="app.py", mode="full",
                      full_access_ok=True)
    assert harness.daemon.plugins.get("pythonator-ai").official, "bundled and verified against official.json"
    harness.ok({"op": "plugin_settings", "plugin": "pythonator-ai", "values": {
        "provider": "Ollama (self-hosted)", "base_url": fake, "model": "test-model"}})
    perms = ["network", "ui", "read_logs", "read_files", "propose_changes"]
    harness.ok({"op": "plugin_grant", "plugin": "pythonator-ai", "permissions": perms, "live": True})
    harness.ok({"op": "plugin_enable", "plugin": "pythonator-ai", "enabled": True})

    def panel_text() -> str:
        panels = [e for e in harness.events if e.get("ev") == "panel" and e.get("plugin") == "pythonator-ai"]
        return json.dumps(panels[-1]["panel"]) if panels and panels[-1]["panel"] else ""

    harness.wait(lambda: "Explain the crash" in panel_text(), timeout=30_000)
    harness.ok({"op": "start", "id": sid, "confirmed": True})
    harness.wait(lambda: "serving" in harness.output(sid), timeout=20_000)
    harness.ok({"op": "plugin_action", "plugin": "pythonator-ai", "action": "quick", "value": "crash"})
    harness.wait(lambda: "Review change to app.py" in panel_text(), timeout=30_000)
    sent = FakeAI.seen[-1]["body"]["messages"][-1]["content"]
    assert "<service_data>" in sent and 'path="app.py"' in sent and "serving 80" in sent
    assert "abcdefghij123456" not in sent, "secrets in the logs and files are removed before sending"
    harness.ok({"op": "plugin_action", "plugin": "pythonator-ai", "action": "propose", "value": "0:app.py"})
    harness.wait(lambda: any(e.get("ev") == "proposal" for e in harness.events), timeout=20_000)
    proposal = next(e["proposal"] for e in harness.events if e.get("ev") == "proposal")
    assert "+PORT = 8080" in proposal["diff"]
    assert "PORT = 80\n" in (project / "app.py").read_text()
    harness.ok({"op": "plugin_proposal", "plugin": "pythonator-ai", "proposal": proposal["id"], "apply": True})
    assert "PORT = 8080\n" in (project / "app.py").read_text()
    harness.wait(lambda: "app.py: Applied" in panel_text(), timeout=20_000)
    assert "Asked Ollama" in _activity(harness, "pythonator-ai")
    harness.ok({"op": "stop", "id": sid})


def test_kill_switch_locks_the_assistant(harness: Any, fake: str) -> None:
    harness.daemon.plugins.vault.use_keychain = False
    harness.ok({"op": "plugin_settings", "plugin": "pythonator-ai", "values": {
        "provider": "Ollama (self-hosted)", "base_url": fake}})
    harness.ok({"op": "plugin_grant", "plugin": "pythonator-ai", "permissions": ["network", "ui"], "live": True})
    harness.ok({"op": "plugin_enable", "plugin": "pythonator-ai", "enabled": True})

    def panel_text() -> str:
        panels = [e for e in harness.events if e.get("ev") == "panel" and e.get("plugin") == "pythonator-ai"]
        return json.dumps(panels[-1]["panel"]) if panels and panels[-1]["panel"] else ""

    harness.wait(lambda: "Kill switch" in panel_text(), timeout=30_000)
    harness.ok({"op": "plugin_action", "plugin": "pythonator-ai", "action": "kill"})
    harness.wait(lambda: "Unlock" in panel_text(), timeout=10_000)
    harness.ok({"op": "plugin_action", "plugin": "pythonator-ai", "action": "ask", "value": "hello"})
    harness.wait(lambda: "locked" in panel_text(), timeout=10_000)
    assert FakeAI.seen == []
    assert (harness.daemon.dirs.plugin_data / "pythonator-ai" / "locked").exists(), "it stays locked after a restart"
