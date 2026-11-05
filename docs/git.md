# Git

Pythonator can clone a repository and run it as a service, and pull new code into it later. It uses the `git`
installed on your computer, with settings that make it safe to point at code you don't know yet.

## Clone a repository

Press **From Git**:

| Field | Meaning |
| --- | --- |
| Repository | `https://host/owner/repo.git`, `ssh://user@host/owner/repo` or `git@host:owner/repo.git` |
| Branch | A branch or tag; empty for the repository's default |
| Clone into | The parent folder; the repository gets a folder of its own inside it |

When the clone is done, the new-service form opens with the folder, the language and the file to run already filled
in. Read the code before you give it more than Sandbox mode with the network off.

Private repositories work with what your `git` already uses: an SSH key, or a credential helper for HTTPS.
Pythonator never asks for a password and never stores one.

## Pull new code

Select the service and press **Packages ▾ → Pull from Git**. Or tick **Pull the newest code before every start** on its **Environment** tab.

Pull fetches and then fast-forwards (`git merge --ff-only`). It never merges, rebases or throws away your changes:
if your copy has commits or changes of its own, Pull stops and says so, and you sort it out with git. The console
shows the range of commits it moved over, `old..new`.

## What Pythonator does to make git safe

| Setting | Why |
| --- | --- |
| Only `https` and `ssh` | `file://`, `ext::` and the other transports that can run commands are off (`protocol.allow=never`) |
| Addresses checked first | An address can't start with `-` (no smuggled options) and must look like one of the three forms above; `--` always separates options from the address |
| Hooks off | `core.hooksPath` points to an empty folder, so a repository can't run its own code on clone or pull |
| No submodules | `--no-recurse-submodules`: a submodule could point anywhere |
| No fsmonitor | `core.fsmonitor=false`: a repository's config can't name a program to run |
| No prompts | `GIT_TERMINAL_PROMPT=0` and no interactive credential helper: a background pull can't hang on a password |
| No system config | `GIT_CONFIG_NOSYSTEM=1` |

The clone itself runs as you, as git always does. Its code doesn't run until you start the service, in the mode
you choose.
