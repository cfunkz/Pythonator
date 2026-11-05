# AI assistant for Pythonator

Ask an AI about one service at a time: why it crashed, what its logs say, what to fix, whether it will survive
running for weeks. It reads the service's recent output and (if you allow it) its files, and can suggest changes,
which you review in Pythonator's own window before anything is written.

It comes with Pythonator 0.0.1 and later as an official plugin, off until you turn it on. Being official, it is
checked against Pythonator's catalog of reviewed plugins (one changed byte and it is treated as third-party) and
gets the permissions it declares once you enable it, without a test mode. Pythonator still refuses its reads of
secrets and dangerous changes, and nothing is written until you press Apply.

## AI services

| Service | Address | Default model | Key |
| --- | --- | --- | --- |
| **Claude (Anthropic)**, the default | `https://api.anthropic.com/v1` (fixed) | `claude-opus-5-5` | [platform.claude.com](https://platform.claude.com/settings/keys) |
| OpenAI | `https://api.openai.com/v1` (fixed) | `gpt-6.1-sol` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google Gemini | Google's OpenAI-compatible endpoint (fixed) | `gemini-flash-latest` | [aistudio.google.com](https://aistudio.google.com/apikey) |
| DeepSeek | `https://api.deepseek.com` (fixed) | `deepseek-v4-flash` | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| Ollama (self-hosted) | `http://localhost:11434/v1` or yours | `qwen2.5-coder` | none |
| Other OpenAI-compatible server (LM Studio, vLLM, llama.cpp, LocalAI…) | yours | set one | optional |

Pick the model in the panel's **Model** list. The list comes from the service itself (chat models only), is kept for
a day and is fetched again with **↻ Refresh**, which also tests the connection. A model typed in the settings is used
until you pick another, and a name the service doesn't know is pointed out.

## Safety

These rules are code, not settings, so they can't be loosened by accident.

**It can't change anything by itself.**
- It answers in text. It has no tools: it can't run commands, open files, browse, or call anything.
- A fix comes as an edit block. **Review change** sends it to Pythonator, which opens a review window with the diff
  and warnings. Nothing is written unless you press **Apply**. Pythonator refuses if the file changed since, and
  keeps a copy of the old file.
- Pythonator refuses some changes outright: scripts and programs (`.sh`, `.bat`, `.ps1`, `.exe`, `Makefile`…),
  secret files, emptying a file or removing most of it, and added code that runs programs or shell commands, deletes
  files, evaluates strings or calls the operating system directly. Adding web addresses, network use or
  dependencies is allowed with a warning.

**Your secrets stay on your computer.**
- Your API key is kept in your system keychain (Windows Credential Manager, macOS Keychain, Secret Service on Linux),
  not in Pythonator's settings, and only this plugin gets it.
- Pythonator never gives the plugin `.env` files, private keys, certificates, token or password files, and hides the
  service's environment variable values in the files it does give.
- Before anything is sent, API keys, tokens, private keys, passwords in config lines, credentials in URLs and webhook
  URLs are replaced by `[secret removed]`. The panel shows how many were removed. A question you type that holds a
  secret isn't sent at all.

**It talks only to the AI service you chose.**
- Claude, OpenAI, Gemini and DeepSeek have fixed addresses. A self-hosted server may be on this computer; on your
  network only if you turn on *Allow plain http:// on my network* (or it uses https).
- Never cloud metadata or link-local addresses, never credentials in the address, no redirects. The address actually
  reached is checked again after connecting.
- Everything from the service is marked for the AI as data, not instructions.

**Failsafes.**
- At most 60 questions an hour by default (300 at most), and 2 seconds between two.
- At most 120 KB sent per question by default (600 at most), answers of up to 16,000 tokens (64,000 at most), 180
  seconds per answer (600 at most). Answers over 400,000 characters are cut.
- One question at a time. **■ Stop** ends an answer at once.
- **⛔ Kill switch** stops the current answer and locks the assistant, even across restarts, until you press
  **Unlock**. Turning the plugin off in Plugins stops its process altogether.
- It runs in its own sandbox, like every plugin. Where this computer has no sandbox (an old Linux kernel without
  Landlock or bubblewrap), official plugins run without one, and its Activity says so; third-party ones don't run.

## Turn it on and try it

1. **Plugins ▾ → Manage plugins…**, select **AI assistant** (*official · off*) and tick **Enabled**.
2. Under **Settings**, choose the **AI service**, paste your **API key** (not needed for Ollama) and press **Save
   settings**. The note under the key says *Saved in your system keychain*. On Windows, Credential Manager →
   Windows Credentials then has an entry `Pythonator:plugin.pythonator-ai.api_key`.
3. **Plugins ▾ → Show plugin panels** and open the **AI assistant** tab. Pick a service, press **Test connection**
   (it should say *Connected … models available*), then **Explain the crash**, **Check the logs** or type a question
   and press Enter.
4. If an answer suggests a change, press **Review change to …**, read the diff, then **Apply change** or **Reject**.

**Ollama on the same Windows PC.** Windows keeps sandboxes off this computer's own servers. Allow it once, in
PowerShell as administrator:

```powershell
CheckNetIsolation LoopbackExempt -a -n=Pythonator.plugin-pythonator-ai
# to undo: CheckNetIsolation LoopbackExempt -d -n=Pythonator.plugin-pythonator-ai
```

Then set **AI service** to *Ollama (self-hosted)*; the address can stay empty (`http://localhost:11434/v1`).

### Things to try

- A bot that crashes at start (`KeyError` on a missing setting): **Explain the crash**, then review the fix.
- Print a fake key in your program's output (`print("token=sk-ant-test-0123456789abcdef0123")`) and ask anything:
  the panel says *1 secret removed*, and the Activity (Plugins → AI assistant) logs what was sent, without the
  contents.
- Type a question that contains a key: it isn't sent.
- Ask it to "delete the logs folder with shutil.rmtree": the review window never opens; the panel shows why it was
  refused.
- Press **⛔ Kill switch**, then ask something: nothing is sent until **Unlock**.

## Remove it

Plugins → AI assistant → **Remove plugin**. This deletes the plugin, its data and its API key from the keychain.

## Files

| File | What's in it |
| --- | --- |
| `plugin.toml` | Name, permissions and settings |
| `plugin.py` | The panel and the flow of a question |
| `pythonator_ai/providers.py` | The AI services, over HTTPS with the standard library |
| `pythonator_ai/safety.py` | Secret removal, allowed addresses, limits |
| `pythonator_ai/context.py` | What is sent, and reading edit blocks back |

Tests: `tests/test_ai_plugin.py` in the Pythonator repository, against a fake AI server.
