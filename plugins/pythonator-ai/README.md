# AI assistant for Pythonator

An official plugin that answers questions about one service at a time: why it crashed, what its logs show, what to
change, and whether it is likely to hold up when left running for weeks. It reads the service's recent output and,
if permitted, its files, and can suggest changes, which are reviewed in Pythonator's own window before anything is
written.

It ships with Pythonator 0.0.1 and later, disabled by default. As an official plugin it is verified against
Pythonator's catalogue of reviewed plugins (a single changed byte makes it third-party) and receives the permissions it
declares once enabled, without test mode. Pythonator still refuses its attempts to read secrets or make dangerous
changes, and nothing is written until the user chooses **Apply change**.

![The AI assistant panel: an answer about a bot's logs, with a suggested change ready for review](../../screenshots/docs/panel-ai.png)

## Supported AI services

| Service | Address | Default model | API key |
| --- | --- | --- | --- |
| **Claude (Anthropic)**, the default | `https://api.anthropic.com/v1` (fixed) | `claude-opus-5-5` | [platform.claude.com](https://platform.claude.com/settings/keys) |
| OpenAI | `https://api.openai.com/v1` (fixed) | `gpt-6.1-sol` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google Gemini | Google's [OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai) (fixed) | `gemini-flash-latest` | [aistudio.google.com](https://aistudio.google.com/apikey) |
| DeepSeek | `https://api.deepseek.com` (fixed) | `deepseek-v4-flash` | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| [Ollama](https://ollama.com) (self-hosted) | `http://localhost:11434/v1` or a custom address | `qwen2.5-coder` | Not needed |
| Other OpenAI-compatible server (LM Studio, vLLM, llama.cpp, LocalAI, …) | Custom | Must be set | Optional |

The model is chosen in the panel's **Model** list, which is fetched from the service itself (chat models only),
cached for a day, and refreshed with the refresh button beside it. A refresh also tests the connection and reports
*Connected to … models to choose from*. A model typed in the settings is used until another is picked, and a name the
service does not recognise is flagged.

## Safety

These rules are implemented in code, not settings, so they cannot be relaxed by accident.

**It cannot change anything by itself.**

- It answers in text only. It has no tools: it cannot run commands, open files, browse or call other services.
- A suggested fix arrives as an edit block. **Review change to …** sends it to Pythonator, which opens a review window
  with the diff and any warnings. Nothing is written unless **Apply change** is chosen there. Pythonator refuses if
  the file has changed since, and keeps a copy of the previous version.
- Pythonator refuses some changes outright: scripts and executables (`.sh`, `.bat`, `.ps1`, `.exe`, `Makefile`, …),
  CI configuration, secret files, emptying a file or removing most of it, and added code that runs programs or shell
  commands, deletes files, evaluates strings or calls the operating system directly. Adding web addresses, network use
  or dependencies is allowed with a warning. See [Proposed changes](../../docs/plugin-api.md#proposed-changes-api-2).

**Secrets stay on the computer.**

- The API key is stored in the system keychain (Windows Credential Manager, macOS Keychain, Secret Service on Linux),
  not in Pythonator's settings, and only this plugin receives it.
- Pythonator never gives the plugin `.env` files, private keys, certificates, or token and password files, and hides
  the service's environment variable values in the files it does provide.
- Before anything is sent, API keys, tokens, private keys, passwords in configuration lines, credentials in URLs and
  webhook URLs are replaced with `[secret removed]`, and the panel shows how many were removed. A typed question that
  contains a secret is not sent at all.

**It only talks to the chosen AI service.**

- Claude, OpenAI, Gemini and DeepSeek have fixed addresses. A self-hosted server may be on the same computer; on the
  local network only over HTTPS, or with *Allow plain http:// on my network* ticked.
- Cloud metadata and link-local addresses, credentials in the address and redirects are all refused. The address
  actually connected to is checked again after connecting.
- Everything taken from the service is marked for the model as data, not instructions.

**Limits.**

- At most 60 questions per hour by default (300 maximum), with at least 2 seconds between questions.
- At most 120 KB sent per question by default (600 KB maximum); answers of up to 16,000 tokens (64,000 maximum) and
  180 seconds (600 maximum). Answers over 400,000 characters are truncated.
- One question at a time. **Stop the answer** ends the current answer immediately.
- The **Kill switch** (lock icon) stops the current answer and locks the assistant, across restarts, until **Unlock**
  is chosen. Disabling the plugin in the Plugins window stops its process entirely.
- Like every plugin, it runs in its own sandbox. Where the computer has no sandbox support (an old Linux kernel
  without Landlock or bubblewrap), official plugins run unsandboxed and the Activity log says so; third-party plugins
  do not run at all.

## Setting it up

1. **Plugins ▾ → Manage plugins…**, select **AI assistant** (*official · off*) and tick **Enabled**.
2. Under **Settings**, choose the **AI service**, paste the **API key** (not needed for Ollama) and choose **Save
   settings**. The note under the key reads *Saved in your system keychain*. On Windows, **Credential Manager →
   Windows Credentials** then lists `Pythonator:plugin.pythonator-ai.api_key`.
3. Open **Plugins ▾ → Show plugin panels** and the **AI assistant** tab. Pick a service and refresh the model list to
   check the connection. Then pick a question from **Quick question…** (*Explain the crash*, *Check the logs*,
   *Suggest a fix*, *Long-run check*, *Explain it*) or type one and press Enter.
4. If an answer suggests a change, choose **Review change to …**, read the diff, then **Apply change** or **Reject**.
   An answer that changes or creates several files (a project layout) has one **Review changes to N files**
   button: one window shows every file, and **Apply all** writes them all or none.

**Ollama on the same Windows PC.** Windows blocks sandboxes from connecting to servers on the same computer. To allow
it once, run in PowerShell as administrator
([CheckNetIsolation](https://learn.microsoft.com/en-us/previous-versions/windows/apps/hh780593(v=win.10))):

```powershell
CheckNetIsolation LoopbackExempt -a -n=Pythonator.plugin-pythonator-ai
# to undo: CheckNetIsolation LoopbackExempt -d -n=Pythonator.plugin-pythonator-ai
```

Then set **AI service** to *Ollama (self-hosted)*; the address can stay empty (`http://localhost:11434/v1`).

### Checks worth trying

- A bot that crashes on start (a `KeyError` on a missing setting): **Explain the crash**, then review the fix.
- Print a fake key in the program's output (`print("token=sk-ant-test-0123456789abcdef0123")`) and ask anything: the
  panel reports *1 secret removed*, and the Activity log (Plugins → AI assistant) records what was sent, without the
  contents.
- Type a question containing a key: it is not sent.
- Ask it to "delete the logs folder with shutil.rmtree": the review window never opens, and the panel explains why the
  change was refused.
- Use the Kill switch, then ask something: nothing is sent until **Unlock**.

## Removing it

Plugins → AI assistant → **Remove plugin** deletes the plugin, its data and its API key from the keychain.

## Files

| File | Contents |
| --- | --- |
| `plugin.toml` | Name, permissions and settings |
| `plugin.py` | The panel and the flow of a question |
| `pythonator_ai/providers.py` | The AI services, over HTTPS with the standard library |
| `pythonator_ai/safety.py` | Secret removal, allowed addresses, limits |
| `pythonator_ai/context.py` | What is sent, and parsing edit blocks in the answer |

Tests: [`tests/test_ai_plugin.py`](../../tests/test_ai_plugin.py) (against a fake AI server) and
[`tests/test_ai_edits.py`](../../tests/test_ai_edits.py) in the Pythonator repository.
