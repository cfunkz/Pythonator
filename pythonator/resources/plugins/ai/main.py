"""AI assistant: answers a question about one service, from what Pythonator chose to send.

It gets the question, the open file (read-only, secrets masked), ruff's problems and, if you granted logs.read for
that service, its last 40 log lines (masked). It has no files and no network of its own: the request to
api.anthropic.com goes through Pythonator (net.connect), which fills in the API key from the OS keychain, so this
code never sees the key. It answers with text and at most five exact-text edits; Pythonator checks each edit and
you apply or reject it.
"""

import json

from pythonator_sdk import plugin

URL = "https://api.anthropic.com/v1/messages"
SYSTEM = (
    "You help someone run a service on their own computer with Pythonator, a service manager. You see one file of the "
    "service, its lint problems and recent log lines. Secrets are masked as •••••••• and you never ask for them. "
    "Answer briefly and plainly, in a few sentences. When a code change would help, propose it as exact-text edits: "
    "'old' is a passage copied exactly from the file that appears in it once, 'new' replaces it. Propose at most "
    "five edits, only for the file you were shown, and none that touch masked values."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                "required": ["old", "new"], "additionalProperties": False,
            },
        },
    },
    "required": ["text", "edits"],
    "additionalProperties": False,
}


def _prompt(req):
    parts = [f"Service: {req.get('service')} ({req.get('runtime')}), command: {' '.join(req.get('command', []))}"]
    f = req.get("file")
    if f:
        parts.append(f"File {f['path']}:\n<file>\n{f['text']}\n</file>")
        probs = req.get("problems") or []
        if probs:
            parts.append("Problems:\n" + "\n".join(f"{p.get('line')}: {p.get('code')} {p.get('message')}" for p in probs))
    logs = req.get("logs")
    if logs:
        parts.append("Last log lines:\n<logs>\n" + "\n".join(logs) + "\n</logs>")
    parts.append("Question: " + req.get("question", ""))
    return "\n\n".join(parts)


@plugin.hook("editor.assist")
def assist(req):
    s = plugin.settings
    body = {
        "model": s.get("model", "claude-opus-5-5"), "max_tokens": int(s.get("max_tokens", 16000)), "system": SYSTEM,
        "output_config": {"effort": s.get("effort", "medium"), "format": {"type": "json_schema", "schema": SCHEMA}},
        "fallbacks": "default", "messages": [{"role": "user", "content": _prompt(req)}],
    }
    headers = {
        "content-type": "application/json", "anthropic-version": "2023-06-01",
        "anthropic-beta": "server-side-fallback-2026-07-01", "x-api-key": "{secret:api_key}",
    }
    try:
        res = plugin.request(URL, method="POST", headers=headers, body=json.dumps(body), timeout=110)
    except (PermissionError, RuntimeError) as e:
        return {"text": f"Couldn't reach the API: {e}", "edits": []}
    status, raw = res.get("status"), res.get("body", "")
    try:
        msg = json.loads(raw)
    except ValueError:
        return {"text": f"The API answered {status} with something that isn't JSON.", "edits": []}
    if status != 200:
        err = (msg.get("error") or {}).get("message", raw[:300]) if isinstance(msg, dict) else raw[:300]
        return {"text": f"The API refused the request ({status}): {err}", "edits": []}
    if msg.get("stop_reason") == "refusal":
        return {"text": "The model declined to answer this one.", "edits": []}
    text = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
    try:
        out = json.loads(text)
    except ValueError:
        return {"text": text[:4000], "edits": []}
    edits = [{"path": (req.get("file") or {}).get("path"), "old": e["old"], "new": e["new"]}
             for e in out.get("edits", []) if isinstance(e, dict) and "old" in e and "new" in e]
    return {"text": str(out.get("text", ""))[:4000], "edits": edits}
