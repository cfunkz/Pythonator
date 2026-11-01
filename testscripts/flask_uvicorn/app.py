from __future__ import annotations

import os
import sys
import time
from flask import Flask, jsonify, request

app = Flask(__name__)

@app.get("/")
def home():
    return """
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Flask Test</title></head>
  <body style="font-family: monospace; background:#0a0a0a; color:#ddd; padding:16px">
    <h2>Flask Test App</h2>
    <p>Endpoints:</p>
    <ul>
      <li><a href="/health">/health</a></li>
      <li><a href="/spam?n=200">/spam?n=200</a> (prints a lot to stdout)</li>
      <li><a href="/slow?sec=2">/slow?sec=2</a> (delayed response)</li>
      <li><a href="/crash">/crash</a> (intentional crash)</li>
    </ul>
    <p>POST /echo (JSON) → echos body</p>
  </body>
</html>
""".strip()

@app.get("/health")
def health():
    return jsonify(ok=True, pid=os.getpid(), python=sys.version)

@app.get("/slow")
def slow():
    sec = float(request.args.get("sec", "1"))
    time.sleep(max(0.0, min(sec, 10.0)))
    print(f"[slow] slept {sec}s", flush=True)
    return jsonify(ok=True, slept=sec)

@app.get("/spam")
def spam():
    n = int(request.args.get("n", "200"))
    n = max(0, min(n, 20000))
    for i in range(n):
        print(f"{i+1} hello", flush=True)
    return jsonify(ok=True, printed=n)

@app.post("/echo")
def echo():
    data = request.get_json(silent=True)
    print(f"[echo] {data}", flush=True)
    return jsonify(ok=True, received=data)

@app.get("/crash")
def crash():
    print("[crash] about to raise", flush=True)
    raise RuntimeError("Intentional crash for runner testing")

if __name__ == "__main__":
    # Use 127.0.0.1 so it's local only
    # threaded=True helps simulate concurrent requests
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
