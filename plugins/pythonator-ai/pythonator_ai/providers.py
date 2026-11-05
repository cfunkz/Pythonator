"""The AI services, spoken to directly over HTTPS with the standard library (plugins can't install packages).

Claude through Anthropic's Messages API; OpenAI, Google Gemini, DeepSeek, Ollama and other servers (LM Studio, vLLM,
llama.cpp, LocalAI...) through the OpenAI-compatible Chat Completions API. Answers stream in as they are written.

Every request: goes only to the provider's address (checked by :func:`safety.check_endpoint` and again after
connecting, against the address actually reached), follows no redirects, has a deadline, reads at most a few MB,
and can be cancelled from another thread.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import safety

MAX_BODY = 4 * 1024 * 1024
MAX_LINE = 1024 * 1024
USER_AGENT = "Pythonator-AI/1.0"


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    kind: str  # "anthropic" or "openai"
    base_url: str  # "" for a self-hosted server: the user sets it
    models: tuple[str, ...]
    needs_key: bool = True
    keys_url: str = ""
    self_hosted: bool = False
    tokens_field: str = "max_tokens"  # OpenAI's own API wants max_completion_tokens

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


PROVIDERS: tuple[Provider, ...] = (
    Provider("claude", "Claude (Anthropic)", "anthropic", "https://api.anthropic.com/v1",
             ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"),
             keys_url="https://platform.claude.com/settings/keys"),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1",
             ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-6.1-terra"),
             keys_url="https://platform.openai.com/api-keys", tokens_field="max_completion_tokens"),
    Provider("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
             ("gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"),
             keys_url="https://aistudio.google.com/apikey"),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com",
             ("deepseek-v4-flash", "deepseek-v4-pro"), keys_url="https://platform.deepseek.com/api_keys"),
    Provider("ollama", "Ollama (self-hosted)", "openai", "http://localhost:11434/v1",
             ("qwen2.5-coder", "llama3.2", "gpt-oss"), needs_key=False, self_hosted=True),
    Provider("custom", "Other OpenAI-compatible server", "openai", "http://localhost:1234/v1", (),
             needs_key=False, self_hosted=True),
)
BY_LABEL = {p.label: p for p in PROVIDERS}
BY_ID = {p.id: p for p in PROVIDERS}
# Claude models that accept the server-side refusal fallback (a declined request is retried on another model).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
EFFORTS = ("low", "medium", "high")


def find(label_or_id: object) -> Provider:
    if isinstance(label_or_id, str):
        return BY_LABEL.get(label_or_id) or BY_ID.get(label_or_id) or PROVIDERS[0]
    return PROVIDERS[0]


class AIError(Exception):
    """Shown to the user as is."""


class Cancelled(AIError):
    def __init__(self) -> None:
        super().__init__("Stopped")


@dataclass
class Request:
    provider: Provider
    model: str
    base_url: str
    api_key: str
    system: str
    messages: list[dict[str, str]]  # {"role": "user"|"assistant", "content": text}
    max_tokens: int
    timeout: int
    effort: str = "medium"
    allow_lan_http: bool = False


@dataclass
class Result:
    text: str = ""
    stop: str = ""  # end_turn, max_tokens, refusal...
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    notes: list[str] = field(default_factory=list)


class Call:
    """One request, cancellable from another thread with :meth:`cancel`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self.cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            conn = self._conn
        if conn is not None and conn.sock is not None:
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.sock.close()

    # -- HTTP --

    def _open(self, url: str, timeout: float, allow_lan_http: bool) -> tuple[http.client.HTTPConnection, str]:
        if why := safety.check_endpoint(url, allow_lan_http=allow_lan_http):
            raise AIError(why)
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
        conn: http.client.HTTPConnection
        if parts.scheme == "https":
            conn = _CheckedHTTPS(host, port, timeout=timeout, context=ssl.create_default_context(),
                                 allow_lan_http=allow_lan_http)
        else:
            conn = _CheckedHTTP(host, port, timeout=timeout, allow_lan_http=allow_lan_http)
        with self._lock:
            if self.cancelled:
                raise Cancelled
            self._conn = conn
        path = parts.path or "/"
        return conn, path

    def request(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None, timeout: float,
                allow_lan_http: bool) -> http.client.HTTPResponse:
        conn, path = self._open(url, timeout, allow_lan_http)
        data = json.dumps(body).encode() if body is not None else None
        all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/event-stream", **headers}
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=all_headers)
            response = conn.getresponse()
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as exc:
            if self.cancelled:
                raise Cancelled from None
            message = _network_message(exc)
            if sys.platform == "win32" and safety.is_loopback(urlsplit(url).hostname or ""):
                message += ("\nOn Windows the plugin's sandbox can't reach servers on this computer until you allow "
                            "it once. In PowerShell as administrator, run: CheckNetIsolation LoopbackExempt -a "
                            "-n=Pythonator.plugin-pythonator-ai")
            raise AIError(message) from None
        if 300 <= response.status < 400:
            raise AIError(f"The server answered with a redirect ({response.status}); redirects aren't followed. "
                          "Check the address.")
        if response.status >= 400:
            raise AIError(_http_message(response))
        return response

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()


class _Checked:
    """After connecting, the address really reached is checked too (a name can resolve anywhere)."""

    allow_lan_http: bool
    sock: socket.socket | None
    host: str

    def _check_peer(self) -> None:
        if self.sock is None:
            return
        address = str(self.sock.getpeername()[0])
        if why := safety.forbidden_ip(address):
            self.sock.close()
            raise AIError(f"Blocked: {self.host} leads to {address}; {why}")


class _CheckedHTTP(_Checked, http.client.HTTPConnection):
    def __init__(self, host: str, port: int | None, *, timeout: float, allow_lan_http: bool) -> None:
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        self.allow_lan_http = allow_lan_http

    def connect(self) -> None:
        super().connect()
        self._check_peer()
        address = str(self.sock.getpeername()[0]) if self.sock else ""
        if not safety.is_loopback(address) and not (self.allow_lan_http and safety.is_private(address)):
            self.close()
            raise AIError(f"Blocked: plain http:// reached {address}, which isn't this computer"
                          + ("" if self.allow_lan_http else " (plain http:// to your network is off)"))


class _CheckedHTTPS(_Checked, http.client.HTTPSConnection):
    def __init__(self, host: str, port: int | None, *, timeout: float, context: ssl.SSLContext,
                 allow_lan_http: bool) -> None:
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout, context=context)
        self.allow_lan_http = allow_lan_http

    def connect(self) -> None:
        super().connect()
        self._check_peer()


def _network_message(exc: BaseException) -> str:
    if isinstance(exc, AIError):
        return str(exc)
    if isinstance(exc, ssl.SSLError):
        return f"Secure connection failed: {exc.reason or exc}"
    if isinstance(exc, socket.timeout | TimeoutError):
        return "The AI service didn't answer in time"
    if isinstance(exc, ConnectionRefusedError):
        return "Nothing answers at that address. Is the server running?"
    if isinstance(exc, socket.gaierror):
        return "Can't find that server (no internet, or a wrong address)"
    if isinstance(exc, PermissionError):
        return "The sandbox blocked the connection: grant the plugin the network permission"
    return f"Network error: {exc}"


def _http_message(response: http.client.HTTPResponse) -> str:
    raw = response.read(64 * 1024)
    detail = ""
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        data = json.loads(raw)
        err = data.get("error", data) if isinstance(data, dict) else data
        detail = str(err.get("message", "") if isinstance(err, dict) else err)
    if not detail:
        detail = raw.decode("utf-8", "replace").strip()[:300]
    hints = {401: "The API key was refused. Check it in the plugin's settings.",
             403: "The API key isn't allowed to do this (or this model).",
             404: "Not found: check the model name and the server address.",
             413: "The request is too large: lower 'Most text sent per question'.",
             429: "The AI service says: too many requests or no credit left. Wait, or check your plan.",
             529: "The AI service is overloaded right now. Try again in a minute."}
    hint = hints.get(response.status, "")
    return f"{response.status}: {detail[:400]}" + (f"\n{hint}" if hint else "")


def _events(response: http.client.HTTPResponse, call: Call, deadline: float) -> Iterator[tuple[str, str]]:
    """Server-sent events: ``(event, data)``. Enforces the deadline and the size limit."""
    event, total = "", 0
    data: list[str] = []
    while True:
        if call.cancelled:
            raise Cancelled
        if time.monotonic() > deadline:
            raise AIError("The answer took longer than the time limit")
        try:
            line = response.readline(MAX_LINE + 1)
        except (OSError, http.client.HTTPException) as exc:
            if call.cancelled:
                raise Cancelled from None
            raise AIError(_network_message(exc)) from None
        if not line:
            if data:
                yield event, "\n".join(data)
            return
        total += len(line)
        if len(line) > MAX_LINE or total > MAX_BODY:
            raise AIError("The answer was too large")
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif text.startswith("event:"):
            event = text[6:].strip()
        elif text.startswith("data:"):
            data.append(text[5:].lstrip())


# -- the two APIs --------------------------------------------------------------------------------------------


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def anthropic_body(req: Request) -> dict[str, Any]:
    body: dict[str, Any] = {"model": req.model, "max_tokens": req.max_tokens, "system": req.system,
                            "messages": req.messages, "stream": True}
    if req.effort in EFFORTS and req.model.startswith("claude-") and not req.model.startswith("claude-haiku-4"):
        body["output_config"] = {"effort": req.effort}
    if req.model in _FALLBACK_MODELS:
        body["fallbacks"] = "default"
    return body


def anthropic_headers(req: Request) -> dict[str, str]:
    headers = {"x-api-key": req.api_key, "anthropic-version": "2026-10-21"}
    if req.model in _FALLBACK_MODELS:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    return headers


def openai_body(req: Request) -> dict[str, Any]:
    body: dict[str, Any] = {"model": req.model, "stream": True,
                            "messages": [{"role": "system", "content": req.system}, *req.messages],
                            req.provider.tokens_field: req.max_tokens}
    if req.provider.id == "openai":
        body["stream_options"] = {"include_usage": True}
    return body


def openai_headers(req: Request) -> dict[str, str]:
    return {"Authorization": f"Bearer {req.api_key}"} if req.api_key else {}


def stream(req: Request, call: Call, on_text: Callable[[str], None]) -> Result:
    """Send ``req`` and call ``on_text`` with each piece of the answer as it arrives. Raises AIError."""
    if req.provider.needs_key and not req.api_key:
        raise AIError(f"Add your {req.provider.label} API key in Plugins → AI assistant → Settings"
                      + (f" (get one at {req.provider.keys_url})" if req.provider.keys_url else ""))
    if not req.model:
        raise AIError("Set a model name in the plugin's settings")
    deadline = time.monotonic() + req.timeout
    result = Result(model=req.model)
    anthropic = req.provider.kind == "anthropic"
    try:
        response = call.request(
            "POST", _url(req.base_url, "/messages" if anthropic else "/chat/completions"),
            anthropic_headers(req) if anthropic else openai_headers(req),
            anthropic_body(req) if anthropic else openai_body(req), min(req.timeout, 120), req.allow_lan_http)
        for _event, data in _events(response, call, deadline):
            if data == "[DONE]":
                break
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            piece = _anthropic_event(msg, result) if anthropic else _openai_event(msg, result)
            if piece:
                if len(result.text) + len(piece) > safety.MAX_ANSWER:
                    result.notes.append("The answer was cut: it was very long")
                    break
                result.text += piece
                on_text(piece)
    finally:
        call.close()
    if result.stop == "refusal":
        result.notes.append("The model declined to answer this.")
    elif result.stop in ("max_tokens", "length"):
        result.notes.append("The answer was cut at the length limit ('Longest answer' in settings).")
    return result


def _anthropic_event(msg: dict[str, Any], result: Result) -> str:
    kind = msg.get("type")
    if kind == "error":
        err = msg.get("error") or {}
        raise AIError(str(err.get("message", "The AI service reported an error")) if isinstance(err, dict) else
                      "The AI service reported an error")
    if kind == "message_start":
        message = msg.get("message") or {}
        usage = message.get("usage") or {}
        result.input_tokens = int(usage.get("input_tokens") or 0)
        result.model = str(message.get("model") or result.model)
    elif kind == "content_block_delta":
        delta = msg.get("delta") or {}
        if delta.get("type") == "text_delta":
            return str(delta.get("text", ""))
    elif kind == "message_delta":
        result.stop = str((msg.get("delta") or {}).get("stop_reason") or result.stop)
        result.output_tokens = int((msg.get("usage") or {}).get("output_tokens") or result.output_tokens)
    return ""


def _openai_event(msg: dict[str, Any], result: Result) -> str:
    if "error" in msg:
        err = msg["error"]
        raise AIError(str(err.get("message", err)) if isinstance(err, dict) else str(err))
    usage = msg.get("usage")
    if isinstance(usage, dict):
        result.input_tokens = int(usage.get("prompt_tokens") or result.input_tokens)
        result.output_tokens = int(usage.get("completion_tokens") or result.output_tokens)
    choices = msg.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    if choice.get("finish_reason"):
        result.stop = str(choice["finish_reason"])
    delta = choice.get("delta") or {}
    return str(delta.get("content") or "") if isinstance(delta, dict) else ""


def list_models(provider: Provider, base_url: str, api_key: str, call: Call, timeout: int,
                allow_lan_http: bool) -> list[str]:
    """The models the service offers: the connection test."""
    if provider.needs_key and not api_key:
        raise AIError(f"Add your {provider.label} API key first")
    headers = ({"x-api-key": api_key, "anthropic-version": "2026-10-21"} if provider.kind == "anthropic"
               else openai_headers(Request(provider, "", base_url, api_key, "", [], 0, 0)))
    try:
        response = call.request("GET", _url(base_url, "/models"), headers, None, min(timeout, 30), allow_lan_http)
        raw = response.read(MAX_BODY + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise AIError(_network_message(exc)) from None
    finally:
        call.close()
    if len(raw) > MAX_BODY:
        raise AIError("The model list was too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise AIError("The server's answer wasn't JSON. Is this the right address?") from None
    items = data.get("data", data.get("models")) if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []
    names = [str(i.get("id") or i.get("name") or "") for i in items if isinstance(i, dict)]
    return sorted(n.removeprefix("models/") for n in names if n)
