"""The assistant's fixed safety rules. None of them is a setting, so none can be turned off by accident.

* **Secrets never leave this computer.** Everything sent to the AI (logs, files, your question) goes through
  :func:`redact`: API keys, tokens, private keys, passwords in config lines and URLs, webhook URLs. A question you
  type that holds a secret isn't sent at all.
* **Only the AI service you picked.** :func:`check_endpoint` allows the fixed address of a known service, or for a
  self-hosted server an address on this computer (or your network, if you allow it). Never cloud metadata or
  link-local addresses, never credentials in the URL. Redirects aren't followed.
* **Limits** (:class:`Limits`): requests per hour and a gap between requests, how much is sent, how long an answer
  may be and take. The settings can lower them; values above the hard caps here are cut to the caps.
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

REDACTED = "[secret removed]"

_PATTERNS: list[re.Pattern[str]] = [re.compile(p) for p in (
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    r"\bsk-ant-[A-Za-z0-9_\-]{16,}",
    r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}",
    r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{22,}\b",
    r"\bglpat-[A-Za-z0-9_\-]{20,}\b",
    r"\bxox[abprse]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bya29\.[0-9A-Za-z_\-]{20,}",
    r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,40}\b",  # Discord bot token
    r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b",  # Telegram bot token
    r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}\b",  # Stripe
    r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b",  # SendGrid
    r"\bhf_[A-Za-z0-9]{30,}\b",  # Hugging Face
    r"\bnpm_[A-Za-z0-9]{30,}\b",
    r"\bpypi-[A-Za-z0-9_\-]{40,}\b",
    r"\bdop_v1_[a-f0-9]{40,}\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
    r"https://(?:hooks\.slack\.com/services|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks)/\S+",
    r"https://api\.telegram\.org/bot[^/\s]+",
    r"\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)=[^;\s]+",
)]
# user:password@ in any URL: keep the address, drop the credentials.
_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE)
# NAME = "value" / NAME: value / --password value, for names that say they're secret.
_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|"
    r"client[_\-]?secret|auth[_\-]?key|credentials?|session[_\-]?key|signing[_\-]?key|bearer)[\w.\-]*"
    r"[\"']?\s*(?:[:=]|=>)\s*|--(?:password|token|secret|api-key)[= ]\s*|\bBearer\s+|\bBasic\s+)"
    r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[A-Za-z0-9+/_\-.~=!@#$%^&*]{8,}(?![\w(]))")
_VALUE_ONLY_WORDS = {"none", "null", "true", "false", "required", "optional", "string", "example", "changeme"}


def redact(text: str, extra: tuple[str, ...] = ()) -> tuple[str, int]:
    """``text`` with secrets replaced by :data:`REDACTED`, and how many were replaced. ``extra``: exact strings to
    remove too (the API key in use)."""
    count = 0
    for secret in extra:
        if secret and len(secret) >= 6 and secret in text:
            count += text.count(secret)
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        count += n
    text, n = _URL_CREDENTIALS.subn(lambda m: f"{m.group(1)}{REDACTED}@", text)
    count += n

    def assignment(m: re.Match[str]) -> str:
        nonlocal count
        value = m.group(2).strip("\"'")
        if value.lower() in _VALUE_ONLY_WORDS or value == REDACTED or value.startswith(("$", "%", "{", "<", "os.")):
            return m.group(0)
        count += 1
        return f"{m.group(1)}{REDACTED}"

    text = _ASSIGNMENT.sub(assignment, text)
    return text, count


# -- where requests may go ------------------------------------------------------------------------------------

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def forbidden_ip(address: str) -> str | None:
    """Why the assistant may never connect to ``address`` (an IP), or None."""
    ip = _ip(address)
    if ip is None:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_link_local or str(ip) in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254"):
        return "link-local and cloud metadata addresses are blocked"
    if ip.is_loopback:
        return None
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "that address is blocked"
    return None


def is_loopback(host: str) -> bool:
    ip = _ip(host)
    return host.lower() in _LOOPBACK_NAMES if ip is None else ip.is_loopback


def is_private(host: str) -> bool:
    ip = _ip(host)
    if ip is None:
        return host.lower().endswith((".local", ".lan", ".home.arpa", ".internal"))
    return ip.is_private and not ip.is_loopback


def check_endpoint(url: str, *, allow_lan_http: bool = False) -> str | None:
    """None when the assistant may send to ``url``; otherwise why not, for the user."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "That isn't a web address"
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        return "Use an http:// or https:// address, like http://localhost:11434/v1"
    if parts.username or parts.password or "@" in parts.netloc:
        return "Don't put a user name or password in the address; use the API key setting"
    if parts.query or parts.fragment:
        return "The address can't have ?query or #fragment parts"
    if port is not None and not 1 <= port <= 65535:
        return "Not a port"
    if why := forbidden_ip(host):
        return f"Blocked: {why}"
    if parts.scheme == "http" and not is_loopback(host):
        if not is_private(host):
            return "Use https:// (plain http:// only to this computer, or your own network if you allow it)"
        if not allow_lan_http:
            return ("Plain http:// to another computer is off. Turn on 'Allow plain http:// on my network' or use "
                    "https://")
    return None


# -- limits ---------------------------------------------------------------------------------------------------

HARD_MAX_PER_HOUR = 300
HARD_MAX_CONTEXT_KB = 600
HARD_MAX_OUTPUT_TOKENS = 64_000
HARD_MAX_TIMEOUT = 600
MIN_GAP = 2.0  # seconds between two requests
MAX_QUESTION = 8000  # characters
MAX_ANSWER = 400_000  # characters kept from one answer


def clamp(value: object, default: int, lo: int, hi: int) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number or default))


@dataclass
class Limits:
    per_hour: int = 60
    context_kb: int = 120
    output_tokens: int = 16_000
    timeout: int = 180

    @classmethod
    def of(cls, settings: dict[str, object]) -> Limits:
        return cls(per_hour=clamp(settings.get("requests_per_hour"), 60, 1, HARD_MAX_PER_HOUR),
                   context_kb=clamp(settings.get("max_context_kb"), 120, 4, HARD_MAX_CONTEXT_KB),
                   output_tokens=clamp(settings.get("max_output_tokens"), 16_000, 256, HARD_MAX_OUTPUT_TOKENS),
                   timeout=clamp(settings.get("timeout"), 180, 10, HARD_MAX_TIMEOUT))


class RateLimit:
    """At most ``per_hour`` requests in any hour, and :data:`MIN_GAP` seconds between two."""

    def __init__(self) -> None:
        self.sent: deque[float] = deque()

    def check(self, per_hour: int, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if self.sent and now - self.sent[-1] < MIN_GAP:
            return "Wait a moment between questions"
        if len(self.sent) >= per_hour:
            wait = int(3600 - (now - self.sent[0])) // 60 + 1
            return f"Hourly limit reached ({per_hour} requests); try again in about {wait} min, or raise it in settings"
        return None

    def take(self, now: float | None = None) -> None:
        self.sent.append(time.monotonic() if now is None else now)

    def used(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        return sum(1 for t in self.sent if now - t <= 3600)
