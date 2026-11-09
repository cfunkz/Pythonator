"""Builder: starter projects for New service.

It only returns file contents. Pythonator writes them into a new, empty folder you pick (through the safe writer),
so this plugin never touches your disk.
"""

from pythonator_sdk import plugin

PY_API = '''"""A small JSON API on the standard library. Run: python main.py"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        return self._send(200, {"hello": "world", "path": self.path})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"listening on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

PY_WORKER = '''"""A worker loop: takes jobs, does them, sleeps. Run: python main.py"""
import time


def work_once(n):
    print(f"job {n}: done", flush=True)


if __name__ == "__main__":
    n = 0
    while True:
        n += 1
        work_once(n)
        time.sleep(5)
'''

PY_JOB = '''"""A job that runs once and exits; give it a schedule in Settings. Run: python main.py"""
import datetime
import pathlib

out = pathlib.Path("out")
out.mkdir(exist_ok=True)
(out / "last-run.txt").write_text(datetime.datetime.now().isoformat() + "\\n")
print("report written to out/last-run.txt", flush=True)
'''

NODE_WEB = '''// A small web server on Node's standard library. Run: node server.js
const http = require("node:http");
const port = Number(process.env.PORT || 3000);
http.createServer((req, res) => {
  if (req.url === "/health") { res.writeHead(200); return res.end("ok"); }
  res.writeHead(200, { "content-type": "text/html" });
  res.end("<h1>Hello from Pythonator</h1>");
}).listen(port, "127.0.0.1", () => console.log(`listening on 127.0.0.1:${port}`));
'''


def _py(name, main, extra=""):
    return {
        "main.py": main,
        "pyproject.toml": f'[project]\\nname = "{name}"\\nversion = "0.1.0"\\nrequires-python = ">=3.11"\\n'
                          f'dependencies = []\\n\\n[tool.ruff]\\nline-length = 100\\n{extra}',
        "README.md": f"# {name}\\n\\nStarted from a Builder template. Pythonator runs it in Sandbox mode.\\n",
        ".gitignore": "__pycache__/\\n.venv/\\nout/\\n",
    }


TEMPLATES = {
    "python-api": ("Python API", "JSON API with a /health route on port 8000", "python", ["main.py"], 8000,
                   lambda n: _py(n, PY_API)),
    "python-worker": ("Python worker", "A loop that takes jobs and sleeps between them", "python", ["main.py"], 0,
                      lambda n: _py(n, PY_WORKER)),
    "python-job": ("Scheduled job", "Runs once and exits; add a schedule", "python", ["main.py"], 0,
                   lambda n: _py(n, PY_JOB)),
    "node-web": ("Node web app", "A web page and /health on port 3000", "node", ["server.js"], 3000,
                 lambda n: {"server.js": NODE_WEB,
                            "package.json": f'{{\\n  "name": "{n}",\\n  "version": "0.1.0",\\n  "private": true\\n}}\\n',
                            "README.md": f"# {n}\\n\\nStarted from a Builder template.\\n"}),
}


@plugin.hook("projects.templates")
def templates(_params):
    return [{"id": k, "title": t[0], "detail": t[1], "runtime": t[2], "command": t[3], "port": t[4]}
            for k, t in TEMPLATES.items()]


@plugin.hook("projects.render")
def render(params):
    t = TEMPLATES.get(params.get("template"))
    if t is None:
        return {"error": "no such template"}
    name = str(params.get("name") or "app")[:48]
    return {"files": t[5](name), "runtime": t[2], "command": t[3], "port": t[4]}
