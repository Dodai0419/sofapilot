---
name: sofapilot
description: Use when the user wants to debloat, clean up, speed up or remove ads from an Android TV, Google TV or Fire TV, or wants to see and control a TV over Wi-Fi. Uses adb, turns apps off without uninstalling them, and can undo every change.
---

# sofapilot

Cleans up an Android TV with adb. Needs Python and adb. It turns apps off and never uninstalls them, and every change can be undone.

This tool has only been tested against a simulator. Say so before the first change on a real TV.

## Steps

1. Connect, then read the TV.
2. Run `plan` and show the person the full list. Changes nothing.
3. Wait for a clear yes. Do not change a TV on your own initiative.
4. Change a few apps first. Check the TV still works: take a screenshot, press Home, and open a streaming app.
5. Change the rest, then check again.
6. If anything broke, run `undo` straight away.

```bash
python sofapilot.py connect 192.168.1.50
python sofapilot.py status
python sofapilot.py plan
python sofapilot.py apply --only com.example.one,com.example.two --yes
python sofapilot.py screenshot -o tv.png
python sofapilot.py undo
python sofapilot.py undo --all
```

Exit codes: `0` success, `1` some apps could not be turned off, `2` refused or bad input.

## With no matching list

Run `lists <brand or model>`, then `plan --list ID`. Nothing is ticked for a list that was not matched. Pick apps with `--only`, and tell the person the list came from a different model.

## MCP tools

`python sofapilot.py mcp` serves these over stdio: `tv_status`, `tv_plan`, `tv_lists`, `tv_apps`, `tv_disable`, `tv_undo`, `tv_history`, `tv_private_dns`, `tv_animations`, `tv_screenshot`, `tv_elements`, `tv_key`, `tv_type`, `tv_tap`, `tv_swipe`, `tv_open`.

## Other commands

- `dns on --yes`: ad-blocking Private DNS. It sends the TV's DNS lookups to that host, so tell the person. `--host` picks another one.
- `animations 0.5 --yes`: faster animations.
- `elements`, `key home`, `type "text"`, `tap X Y`, `swipe X1 Y1 X2 Y2`, `open PACKAGE`: drive the TV.
- `history`: every recorded change.
- `--serial SERIAL` before the command, when several TVs are connected.

## Rules

- Never suggest `pm uninstall` or a raw `adb shell` to get past a refusal. A refusal means the TV needs that app.
- Do not type passwords or sign in on the TV for the person.
- Do not claim the TV got faster unless you measured it.
- Apps can come back after a system update. Run `plan` again after one.
