"""The files of the Project builder's own templates: bots and small apps, each the library's documented minimal
example. ``{port}`` is replaced with the service's port; nothing else in them is filled in."""

from __future__ import annotations

import json


def package_json(name: str, dependencies: dict[str, str], main: str = "index.js", *, bun: bool = False,
                 engines: str = ">=22", scripts: dict[str, str] | None = None) -> str:
    """``scripts`` given: a framework's project (``type: module``, its own scripts, no ``main``)."""
    if bun:
        return json.dumps({"name": name, "version": "0.1.0", "private": True, "module": main, "type": "module",
                           "scripts": {"start": f"bun run {main}", "dev": f"bun run --watch {main}"},
                           "dependencies": dependencies}, indent=2) + "\n"
    if scripts:
        return json.dumps({"name": name, "version": "0.1.0", "private": True, "type": "module",
                           "scripts": scripts, "engines": {"node": engines},
                           "dependencies": dependencies}, indent=2) + "\n"
    return json.dumps({"name": name, "version": "0.1.0", "private": True, "main": main,
                       "scripts": {"start": f"node {main}"}, "engines": {"node": engines},
                       "dependencies": dependencies}, indent=2) + "\n"


DISCORD_PY = '''"""A Discord bot with discord.py (discordpy.readthedocs.io): it answers /ping.

Make the bot at discord.com/developers/applications (New Application, then Bot, then Reset Token), put the token
in DISCORD_TOKEN in Configure → Environment, and invite it with OAuth2 → URL Generator (scopes bot and
applications.commands)."""

import asyncio
import contextlib
import os
import signal
import sys

import discord
from discord import app_commands

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()


class Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()  # registers /ping; Discord can take a minute to show it


bot = Bot()


@bot.tree.command(description="Check that the bot answers")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)} ms")


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}", flush=True)


async def main() -> None:
    # Stop sends SIGTERM on Linux and macOS (Ctrl+C on Windows): log out of Discord cleanly on either.
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers here
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(bot.close()))
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        print("No DISCORD_TOKEN yet. Set it in Configure → Environment, then press Start.", file=sys.stderr)
        sys.exit(0)
    discord.utils.setup_logging()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
'''

DISCORD_JS = '''// A Discord bot with discord.js (discord.js.org): it answers /ping.
//
// Make the bot at discord.com/developers/applications (New Application, then Bot, then Reset Token), put the
// token in DISCORD_TOKEN in Configure → Environment, and invite it with OAuth2 → URL Generator (scopes bot and
// applications.commands).
const { Client, Events, GatewayIntentBits, REST, Routes, SlashCommandBuilder } = require("discord.js");

const token = (process.env.DISCORD_TOKEN || "").trim();
if (!token) {
  console.error("No DISCORD_TOKEN yet. Set it in Configure → Environment, then press Start.");
  process.exit(0);
}

const ping = new SlashCommandBuilder().setName("ping").setDescription("Check that the bot answers");
const client = new Client({ intents: [GatewayIntentBits.Guilds] });

client.once(Events.ClientReady, async (ready) => {
  console.log(`Logged in as ${ready.user.tag}`);
  // Registers /ping; Discord can take a minute to show it.
  await new REST().setToken(token).put(Routes.applicationCommands(ready.user.id), { body: [ping.toJSON()] });
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (interaction.isChatInputCommand() && interaction.commandName === "ping") {
    await interaction.reply(`Pong! ${client.ws.ping} ms`);
  }
});

// Stop sends SIGTERM on Linux and macOS (Ctrl+C on Windows): log out of Discord cleanly on either.
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, async () => {
    await client.destroy();
    process.exit(0);
  });
}

client.login(token);
'''

TELEGRAM_PY = '''"""A Telegram bot with python-telegram-bot (docs.python-telegram-bot.org): /start, and it echoes text.

Make the bot with @BotFather in Telegram (/newbot), then put its token in TELEGRAM_TOKEN in Configure →
Environment. It asks Telegram for new messages (long polling), so it needs no public address."""

import os
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Hi! Send me a message and I'll send it back.")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        await update.message.reply_text(update.message.text)


def main() -> None:
    if not TOKEN:
        print("No TELEGRAM_TOKEN yet. Set it in Configure → Environment, then press Start.", file=sys.stderr)
        sys.exit(0)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    print("Running: send your bot a message in Telegram", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
'''

TELEGRAM_JS = '''// A Telegram bot with grammY (grammy.dev): /start, and it echoes text.
//
// Make the bot with @BotFather in Telegram (/newbot), then put its token in TELEGRAM_TOKEN in Configure →
// Environment. It asks Telegram for new messages (long polling), so it needs no public address.
const { Bot } = require("grammy");

const token = (process.env.TELEGRAM_TOKEN || "").trim();
if (!token) {
  console.error("No TELEGRAM_TOKEN yet. Set it in Configure → Environment, then press Start.");
  process.exit(0);
}

const bot = new Bot(token);
bot.command("start", (ctx) => ctx.reply("Hi! Send me a message and I'll send it back."));
bot.on("message:text", (ctx) => ctx.reply(ctx.message.text));
bot.catch((err) => console.error("Error while handling an update:", err.error));
// Stop sends SIGTERM on Linux and macOS (Ctrl+C on Windows). Stopping cleanly tells Telegram which updates were
// handled, so none is answered twice after a restart.
process.once("SIGINT", () => bot.stop());
process.once("SIGTERM", () => bot.stop());
bot.start({ onStart: (me) => console.log(`Running as @${me.username}: send it a message in Telegram`) });
'''

WHATSAPP_HELP = """A WhatsApp bot on the WhatsApp Business Cloud API, Meta's official API
(developers.facebook.com/docs/whatsapp/cloud-api): it answers each text message.

In Configure → Environment set WHATSAPP_TOKEN (an access token of your Meta app), WHATSAPP_VERIFY_TOKEN (a secret
you make up and also type into the app's webhook settings) and, to check that each call really comes from Meta,
WHATSAPP_APP_SECRET (App settings → Basic). Meta calls the webhook over HTTPS from the internet, so point a tunnel
(for example cloudflared) at http://127.0.0.1:{port}/webhook and give Meta the tunnel's address."""

WHATSAPP_PY = f'''"""{WHATSAPP_HELP}"""

import hashlib
import hmac
import json
import os
import urllib.request

from flask import Flask, abort, request

GRAPH = "https://graph.facebook.com/v23.0"
TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
VERIFY = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
SECRET = os.environ.get("WHATSAPP_APP_SECRET", "").strip()

app = Flask(__name__)


@app.get("/webhook")
def verify():
    """Meta checks the webhook once, when you add it."""
    token = request.args.get("hub.verify_token", "")
    if VERIFY and request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(token, VERIFY):
        return request.args.get("hub.challenge", "")
    abort(403)


@app.post("/webhook")
def receive():
    if SECRET:
        mine = "sha256=" + hmac.new(SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), mine):
            abort(403)
    data = request.get_json(silent=True) or {{}}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {{}})
            phone = str(value.get("metadata", {{}}).get("phone_number_id", ""))
            for message in value.get("messages", []):
                if message.get("type") == "text" and phone.isdigit():
                    reply(phone, str(message.get("from", "")), "You said: " + message["text"]["body"])
    return "ok"


def reply(phone: str, to: str, text: str) -> None:
    body = json.dumps({{"messaging_product": "whatsapp", "to": to, "type": "text",
                       "text": {{"body": text[:4096]}}}}).encode()
    sent = urllib.request.Request(f"{{GRAPH}}/{{phone}}/messages", data=body, method="POST",
                                  headers={{"Authorization": f"Bearer {{TOKEN}}", "Content-Type": "application/json"}})
    with urllib.request.urlopen(sent, timeout=15) as response:
        response.read()


if __name__ == "__main__":
    if not TOKEN or not VERIFY:
        print("Set WHATSAPP_TOKEN and WHATSAPP_VERIFY_TOKEN in Configure → Environment, then press Start.")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "{{port}}")))
'''

WHATSAPP_JS = "// " + WHATSAPP_HELP.replace("\n", "\n// ") + '''
const crypto = require("node:crypto");
const express = require("express");

const GRAPH = "https://graph.facebook.com/v23.0";
const TOKEN = (process.env.WHATSAPP_TOKEN || "").trim();
const VERIFY = (process.env.WHATSAPP_VERIFY_TOKEN || "").trim();
const SECRET = (process.env.WHATSAPP_APP_SECRET || "").trim();

const same = (a, b) => a.length === b.length && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
const app = express();
app.use(express.json({ verify: (req, _res, raw) => { req.raw = raw; } }));

// Meta checks the webhook once, when you add it.
app.get("/webhook", (req, res) => {
  const token = String(req.query["hub.verify_token"] || "");
  if (VERIFY && req.query["hub.mode"] === "subscribe" && same(token, VERIFY)) {
    res.send(String(req.query["hub.challenge"] || ""));
  } else {
    res.sendStatus(403);
  }
});

app.post("/webhook", async (req, res) => {
  if (SECRET) {
    const mine = "sha256=" + crypto.createHmac("sha256", SECRET).update(req.raw || "").digest("hex");
    if (!same(String(req.get("X-Hub-Signature-256") || ""), mine)) return res.sendStatus(403);
  }
  res.send("ok");
  for (const entry of req.body.entry || []) {
    for (const change of entry.changes || []) {
      const value = change.value || {};
      const phone = String((value.metadata || {}).phone_number_id || "");
      for (const message of value.messages || []) {
        if (message.type === "text" && /^\\d+$/.test(phone)) {
          await reply(phone, String(message.from || ""), "You said: " + message.text.body);
        }
      }
    }
  }
});

async function reply(phone, to, text) {
  const answer = await fetch(`${GRAPH}/${phone}/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messaging_product: "whatsapp", to, type: "text", text: { body: text.slice(0, 4096) } }),
  });
  if (!answer.ok) console.error("WhatsApp refused the reply:", answer.status, await answer.text());
}

if (!TOKEN || !VERIFY) {
  console.log("Set WHATSAPP_TOKEN and WHATSAPP_VERIFY_TOKEN in Configure → Environment, then press Start.");
}
const listenOn = Number(process.env.PORT || {port});
// Express 5 hands a failed listen (port in use) to this callback: throw it, so the app exits with an error.
app.listen(listenOn, "127.0.0.1", (err) => {
  if (err) throw err;
  console.log(`Webhook on http://127.0.0.1:${listenOn}/webhook`);
});
'''

EXPRESS_JS = '''// An Express app (expressjs.com): a JSON API to build on.
const express = require("express");

const app = express();
app.use(express.json());

app.get("/", (_req, res) => res.json({ message: "Hello World" }));
app.get("/health", (_req, res) => res.json({ ok: true }));

const listenOn = Number(process.env.PORT || {port});
// Express 5 hands a failed listen (port in use) to this callback: throw it, so the app exits with an error.
app.listen(listenOn, "127.0.0.1", (err) => {
  if (err) throw err;
  console.log(`Listening on http://127.0.0.1:${listenOn}/`);
});
'''

WORKER_PY = '''"""A long-running Python worker: it does one job every INTERVAL seconds (60 unless set in Configure →
Environment) and stops cleanly when you press Stop. Put your own work in do_work()."""

import logging
import os
import signal
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")
stop = threading.Event()


def do_work() -> None:
    log.info("Working…")


def main() -> None:
    interval = float(os.environ.get("INTERVAL", "60") or 60)
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    log.info("Started: one job every %s s", interval)
    while not stop.is_set():
        try:
            do_work()
        except Exception:
            log.exception("The job failed; trying again next time")
        stop.wait(interval)
    log.info("Stopped")


if __name__ == "__main__":
    main()
'''

# -- 0.0.15: more web apps, for every runtime Pythonator runs -------------------------------------------------

FASTIFY_JS = '''// A Fastify app (fastify.dev): a JSON API to build on.
const Fastify = require("fastify");

const app = Fastify({ logger: true });

app.get("/", async () => ({ message: "Hello World" }));
app.get("/health", async () => ({ ok: true }));

const listenOn = Number(process.env.PORT || {port});
app.listen({ port: listenOn, host: "127.0.0.1" }).catch((err) => {
  app.log.error(err);
  process.exit(1);
});
'''

HONO_JS = '''// A Hono app on Node.js (hono.dev, with @hono/node-server): a small, fast web framework.
const { Hono } = require("hono");
const { serve } = require("@hono/node-server");

const app = new Hono();
app.get("/", (c) => c.json({ message: "Hello World" }));
app.get("/health", (c) => c.json({ ok: true }));

const listenOn = Number(process.env.PORT || {port});
serve({ fetch: app.fetch, port: listenOn, hostname: "127.0.0.1" }, (info) =>
  console.log(`Listening on http://127.0.0.1:${info.port}/`));
'''

KOA_JS = '''// A Koa app (koajs.com): middleware in a small core.
const Koa = require("koa");

const app = new Koa();
app.use(async (ctx) => {
  ctx.body = ctx.path === "/health" ? { ok: true } : { message: "Hello World" };
});

const listenOn = Number(process.env.PORT || {port});
app.listen(listenOn, "127.0.0.1", () => console.log(`Listening on http://127.0.0.1:${listenOn}/`));
'''

WORKER_JS = '''// A long-running Node.js worker: it does one job every INTERVAL seconds (60 unless set in Configure →
// Environment) and stops cleanly when you press Stop. Put your own work in doWork().
const interval = Number(process.env.INTERVAL || 60) * 1000;

async function doWork() {
  console.log(new Date().toISOString(), "Working…");
}

let timer;
async function tick() {
  try {
    await doWork();
  } catch (err) {
    console.error("The job failed; trying again next time:", err);
  }
  timer = setTimeout(tick, interval);
}

for (const signal of ["SIGTERM", "SIGINT", "SIGBREAK"]) {
  process.on(signal, () => {
    clearTimeout(timer);
    console.log("Stopped");
    process.exit(0);
  });
}
console.log(`Started: one job every ${interval / 1000} s`);
tick();
'''

STREAMLIT_PY = '''"""A Streamlit app (docs.streamlit.io): a data app in plain Python. Pythonator runs it with
``python app.py``, which starts ``streamlit run`` on this computer only; ``streamlit run app.py`` works too."""

import os
import sys

import streamlit as st

st.title("Hello, Streamlit")
name = st.text_input("Your name", "World")
st.write(f"Hello, {name}!")
st.line_chart([1, 3, 2, 4, 3, 5])

if __name__ == "__main__":
    from streamlit import runtime

    if not runtime.exists():  # started as a script, not by Streamlit: start Streamlit with this file
        from streamlit.web import cli

        sys.argv = ["streamlit", "run", __file__, "--server.address", "127.0.0.1",
                    "--server.port", os.environ.get("PORT", "{port}"), "--server.headless", "true",
                    "--browser.gatherUsageStats", "false"]
        sys.exit(cli.main())
'''

GRADIO_PY = '''"""A Gradio app (gradio.app): a web UI for a Python function, often a machine-learning model."""

import os

import gradio as gr


def greet(name: str) -> str:
    return f"Hello, {name}!"


demo = gr.Interface(fn=greet, inputs="text", outputs="text", title="Hello, Gradio")

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=int(os.environ.get("PORT", "{port}")))
'''

LITESTAR_PY = '''"""A Litestar app (litestar.dev): a typed ASGI API. Pythonator runs it with ``python app.py``
(Uvicorn)."""

import os

import uvicorn
from litestar import Litestar, get


@get("/")
async def index() -> dict[str, str]:
    return {"message": "Hello World"}


@get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


app = Litestar(route_handlers=[index, health])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "{port}")))
'''

GO_MOD = "module {module}\n\ngo 1.25\n"  # Gin 1.12 needs 1.25

GO_HTTP = '''// A Go web server with the standard library (pkg.go.dev/net/http): nothing to install.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "{port}"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"message": "Hello World"})
	})
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})
	log.Printf("Listening on http://127.0.0.1:%s/", port)
	log.Fatal(http.ListenAndServe("127.0.0.1:"+port, mux))
}
'''

GIN_GO = '''// A Gin web server (gin-gonic.com): a fast HTTP framework for Go.
package main

import (
	"net/http"
	"os"

	"github.com/gin-gonic/gin"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "{port}"
	}
	r := gin.Default()
	r.GET("/", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"message": "Hello World"}) })
	r.GET("/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"ok": true}) })
	r.Run("127.0.0.1:" + port)
}
'''

AXUM_TOML = '''[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[dependencies]
axum = "0.8"
tokio = { version = "1", features = ["macros", "rt-multi-thread", "net"] }
serde_json = "1"
'''

AXUM_RS = '''// An Axum web server (docs.rs/axum): Rust's async web framework, on Tokio.
use axum::{routing::get, Json, Router};
use serde_json::{json, Value};

async fn index() -> Json<Value> {
    Json(json!({ "message": "Hello World" }))
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "{port}".to_string());
    let app = Router::new()
        .route("/", get(index))
        .route("/health", get(|| async { Json(json!({ "ok": true })) }));
    let listener = tokio::net::TcpListener::bind(format!("127.0.0.1:{}", port)).await.unwrap();
    println!("Listening on http://127.0.0.1:{}/", port);
    axum::serve(listener, app).await.unwrap();
}
'''

ELYSIA_TS = '''// An Elysia app on Bun (elysiajs.com): a fast, typed web framework made for Bun.
import { Elysia } from "elysia";

const port = Number(process.env.PORT || {port});
new Elysia()
  .get("/", () => ({ message: "Hello World" }))
  .get("/health", () => ({ ok: true }))
  .listen({ hostname: "127.0.0.1", port });

console.log(`Listening on http://127.0.0.1:${port}/`);
'''

HONO_BUN_TS = '''// A Hono app on Bun (hono.dev): a small, fast web framework.
import { Hono } from "hono";

const app = new Hono();
app.get("/", (c) => c.json({ message: "Hello World" }));
app.get("/health", (c) => c.json({ ok: true }));

const port = Number(process.env.PORT || {port});
Bun.serve({ fetch: app.fetch, hostname: "127.0.0.1", port });
console.log(`Listening on http://127.0.0.1:${port}/`);
'''

DENO_TS = '''// A Deno web server (docs.deno.com): Deno.serve, nothing to install.
const port = Number(Deno.env.get("PORT") ?? "{port}");

Deno.serve({ hostname: "127.0.0.1", port }, (req) => {
  const path = new URL(req.url).pathname;
  return Response.json(path === "/health" ? { ok: true } : { message: "Hello World" });
});
'''

DENO_JSON = '''{
  "tasks": {
    "dev": "deno run --allow-net --allow-env --watch main.ts"
  }
}
'''

JAVA_MAIN = '''// A Java web server with the JDK's own HttpServer (JDK 11+ runs this file as is: java Main.java).
import com.sun.net.httpserver.HttpServer;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

public class Main {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "{port}"));
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/", exchange -> {
            String path = exchange.getRequestURI().getPath();
            byte[] body = (path.equals("/health") ? "{\\"ok\\":true}" : "{\\"message\\":\\"Hello World\\"}")
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
            }
        });
        server.start();
        System.out.println("Listening on http://127.0.0.1:" + port + "/");
    }
}
'''

PHP_INDEX = '''<?php
// A PHP app on PHP's built-in web server (php.net/features.commandline.webserver), for development.
header('Content-Type: application/json');
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
echo json_encode($path === '/health' ? ['ok' => true] : ['message' => 'Hello World']);
'''

SINATRA_GEMFILE = '''source "https://rubygems.org"

gem "sinatra", "~> 4.2"
gem "rackup", "~> 2.2"
gem "puma", "~> 8.0", ">= 8.0.2"
'''

SINATRA_RB = '''# A Sinatra app (sinatrarb.com): a small Ruby web framework. Pythonator runs it with ``ruby app.rb``.
require "bundler/setup"
require "sinatra"
require "json"

set :bind, "127.0.0.1"
set :port, Integer(ENV.fetch("PORT", "{port}"))

get "/" do
  content_type :json
  { message: "Hello World" }.to_json
end

get "/health" do
  content_type :json
  { ok: true }.to_json
end
'''


# Astro's minimal starter (github.com/withastro/astro, examples/minimal), written here: create-astro fetches it
# from GitHub, which a firewall or GitHub's rate limit for anonymous downloads can refuse.
ASTRO_CONFIG = """// @ts-check
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({});
"""

ASTRO_INDEX = """---
---

<html lang="en">
\t<head>
\t\t<meta charset="utf-8" />
\t\t<meta name="viewport" content="width=device-width" />
\t\t<meta name="generator" content={Astro.generator} />
\t\t<title>Astro</title>
\t</head>
\t<body>
\t\t<h1>Astro</h1>
\t</body>
</html>
"""

ASTRO_TSCONFIG = """{
  "extends": "astro/tsconfigs/strict",
  "include": [".astro/types.d.ts", "**/*"],
  "exclude": ["dist"]
}
"""
