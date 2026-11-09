"""Secret masking at one choke point.
Every line of output passes through a Masker before it reaches a log file, the console, a plugin, the AI
assistant or a webhook. Masking is by value (the actual secret strings this service was given) plus shapes that
are always secrets wherever they come from: passwords inside URLs, bearer tokens, well-known token formats (GitHub,
GitLab, Slack, AWS, Google, Stripe, OpenAI/Anthropic-style `sk-` keys, JWTs) and the body of a PEM private key."""

from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "••••••••"
MIN_LEN = 4

_URL_PW = re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]*://[^:/@\s]+:)(?P<pw>[^@\s/]+)(?P<at>@)")
_BEARER = re.compile(r"(?i)(?P<pre>\b(?:bearer|token|authorization:)\s+)(?P<pw>[A-Za-z0-9._~+/=-]{12,})")

_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_TOKEN_HINTS = ("gh", "glpat-", "xox", "AKIA", "ASIA", "AIza", "k_live_", "k_test_", "sk-", "eyJ")
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_LINE = re.compile(r"[^\r\n]+")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

_SECRET_NAME = re.compile(
    r"(?i)(TOKEN|SECRET|PASSW(OR)?D|PASS$|API_?KEY|PRIVATE|CREDENTIAL|(^|_)KEY$|AUTH|DSN|COOKIE|SESSION|WEBHOOK|"
    r"DATABASE_URL|_PAT$|ACCESS_KEY|SIGNING|SALT)"
)
_PLAIN = re.compile(r"^(?:[0-9.:]+|true|false|yes|no|on|off|[a-z]+|[a-z0-9.-]+\.[a-z]{2,}|localhost)$", re.IGNORECASE)

def looks_secret(name: str) -> bool:
    """Variable names whose values are masked like secrets, wherever they were set."""
    return bool(_SECRET_NAME.search(name))

def secret_values(env: dict[str, str], *, everything: bool = False) -> list[str]:
    """Values to mask from a service's variables. For the console and logs: values of secret-looking names. For
    anything that leaves through a plugin (`everything`): every value of 6+ characters except numbers, booleans,
    plain words, host names and addresses, since a plugin may forward what it reads."""
    if everything:
        return [v for v in env.values() if len(v) >= 6 and not _PLAIN.fullmatch(v)]
    return [v for k, v in env.items() if looks_secret(k)]

class Masker:
    def __init__(self, values: Iterable[str] = ()) -> None:
        vals = sorted({v for v in values if v and len(v) >= MIN_LEN}, key=len, reverse=True)
        self._values = tuple(vals)
        self._re = re.compile("|".join(re.escape(v) for v in vals)) if vals else None
        self._pem = False  # inside a private key printed over several lines


    def __bool__(self) -> bool:
        return True

    def mask(self, text: str) -> str:
        if self._pem or "PRIVATE KEY-----" in text:
            text = self._mask_pem(text)
        if self._re is not None:
            text = self._re.sub(MASK, text)
        if "://" in text:
            text = _URL_PW.sub(lambda m: m["pre"] + MASK + m["at"], text)
        lt = text.lower()
        if "bearer" in lt or "token" in lt or "authorization" in lt:
            text = _BEARER.sub(lambda m: m["pre"] + MASK, text)
        if any(h in text for h in _TOKEN_HINTS):
            text = _TOKENS.sub(MASK, text)
        return text

    def _mask_pem(self, text: str) -> str:
        """Everything between BEGIN and END of a private key, across lines and calls; the markers stay readable."""
        out, pos = [], 0
        while pos <= len(text):
            if not self._pem:
                m = _PEM_BEGIN.search(text, pos)
                if m is None:
                    out.append(text[pos:])
                    break
                out.append(text[pos:m.end()])
                pos, self._pem = m.end(), True
            else:
                m = _PEM_END.search(text, pos)
                end = m.start() if m else len(text)
                body = text[pos:end]
                out.append(_LINE.sub(MASK, body))
                if m is None:
                    break
                out.append(m.group())
                pos, self._pem = m.end(), False
        return "".join(out)
