# Command line

Pythonator is used from its window. `pythonatorctl` only does the two things you may need outside it.

```bash
pythonatorctl version                    # or: pythonatorctl --version
pythonatorctl uninstall                  # remove Pythonator, keep your settings
pythonatorctl uninstall --remove-data    # remove your settings, logs and service list too
```

`uninstall`, in order:

1. stops every service and the background service, and takes back every folder permission Pythonator gave
   (Windows);
2. removes the login task, systemd user unit or LaunchAgent;
3. removes the Start menu entry and the Settings → Apps entry (Windows), the app menu entry (Linux) or
   `~/Applications/Pythonator.app` (macOS), and the `pythonator` and `pythonatorctl` links;
4. removes the install folder (on Windows a hidden process finishes this a few seconds after `pythonatorctl`
   exits, because it runs from that folder).

Your services' own folders are never touched.

## Uninstall

| Installed with | Remove it |
| --- | --- |
| Windows `setup.exe` | Settings → Apps → Pythonator → Uninstall. It stops everything, takes back every folder permission and removes the logon task first. `pythonatorctl uninstall` does the same. |
| macOS `.dmg` | `/Applications/Pythonator.app/Contents/Resources/python/bin/python3 -m pythonator.ctl.main uninstall`, which also removes the app. Dragging the app to the Trash leaves the LaunchAgent behind. |
| Linux `.deb` | `pythonatorctl uninstall` (stops it and keeps or, with `--remove-data`, deletes your settings), then `sudo apt remove pythonator`. |
| A portable `.zip` or `.tar.gz` | `pythonatorctl uninstall` from that folder (`bin\pythonatorctl.cmd` on Windows), then delete the folder. |

`pythonatorctl shutdown` and `pythonatorctl service install|remove|status` exist for the installers and the
portable downloads, and aren't listed in the help.
