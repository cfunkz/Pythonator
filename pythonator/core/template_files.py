"""The files of the Project builder's own templates: bots and small apps, each the library's documented minimal
example. ``{port}`` is replaced with the service's port; nothing else in them is filled in."""

from __future__ import annotations

import json


def package_json(name: str, dependencies: dict[str, str], main: str = "index.js") -> str:
    return json.dumps({"name": name, "version": "0.1.0", "private": True, "main": main,
                       "scripts": {"start": f"node {main}"}, "engines": {"node": ">=20"},
                       "dependencies": dependencies}, indent=2) + "\n"


DISCORD_PY = '''"""A Discord bot with discord.py (discordpy.readthedocs.io): it answers /ping.

Make the bot at discord.com/developers/applications (New Application, then Bot, then Reset Token), put the token
in DISCORD_TOKEN in Configure → Environment, and invite it with OAuth2 → URL Generator (scopes bot and
applications.commands)."""

import os
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


if __name__ == "__main__":
    if not TOKEN:
        print("No DISCORD_TOKEN yet. Set it in Configure → Environment, then press Start.", file=sys.stderr)
        sys.exit(0)
    bot.run(TOKEN)
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
app.listen(listenOn, "127.0.0.1", () => console.log(`Webhook on http://127.0.0.1:${listenOn}/webhook`));
'''

EXPRESS_JS = '''// An Express app (expressjs.com): a JSON API to build on.
const express = require("express");

const app = express();
app.use(express.json());

app.get("/", (_req, res) => res.json({ message: "Hello World" }));
app.get("/health", (_req, res) => res.json({ ok: true }));

const listenOn = Number(process.env.PORT || {port});
app.listen(listenOn, "127.0.0.1", () => console.log(`Listening on http://127.0.0.1:${listenOn}/`));
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
