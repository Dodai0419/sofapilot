#!/usr/bin/env python3
"""A pretend Android TV that answers like adb, for the demo and tests. State lives in the JSON file named by FAKE_TV."""
import json, os, pathlib, re, shlex, sys

DEFAULT = {
    "serial": "192.168.1.50:5555",
    "online": True,
    "props": {"ro.product.manufacturer": "TPV", "ro.product.model": "Philips Google TV TA1", "ro.product.device": "PH1M_WW_9972", "ro.build.version.sdk": "31", "ro.build.version.release": "12"},
    "packages": {
        "com.google.android.apps.tv.launcherx": True, "com.android.systemui": True, "com.android.settings": True, "com.google.android.gms": True, "org.droidtv.settings": True,
        "com.android.vending": True, "com.google.android.feedback": True, "com.google.android.partnersetup": True, "com.google.android.apps.tv.dreamx": True, "tv.anoki.acr.anokiacroptin": True,
        "org.droidtv.candeebug": True, "org.droidtv.gel": True, "tv.samba.ssm": True, "com.tpv.nftgallery": True, "org.droidtv.demome": True, "tv.twitch.android.app": True, "com.netflix.ninja": True, "com.spotify.tv.android": True, "com.old.already.off": False,
    },
    "third": ["com.spotify.tv.android"],
    "settings": {"private_dns_mode": "off", "private_dns_specifier": "null", "window_animation_scale": "1.0", "transition_animation_scale": "1.0", "animator_duration_scale": "1.0"},
    "log": [],
    "fail": [],
}
UI = '<?xml version="1.0"?><hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1920,1080]"><node index="1" text="Netflix" resource-id="app:id/tile" class="android.widget.Button" content-desc="Open Netflix" clickable="true" bounds="[100,200][300,400]"/></node></hierarchy>UI hierarchy dumped to: /dev/tty\n'
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" + "1f15c489" + "0000000d49444154789c6360f8ffff3f0005fe02fea73581e40000000049454e44ae426082")


def main():
    STATE = pathlib.Path(os.environ["FAKE_TV"])
    state = json.loads(STATE.read_text()) if STATE.exists() else json.loads(json.dumps(DEFAULT))
    args = sys.argv[1:]
    if args[:1] == ["-s"]:
        args = args[2:]
    out = shell(state, args)
    STATE.write_text(json.dumps(state))
    if isinstance(out, bytes):
        sys.stdout.buffer.write(out)
    else:
        sys.stdout.write(out)


def shell(state, args):
    if args[0] == "devices":
        return "List of devices attached\n" + (f"{state['serial']}\tdevice\n" if state["online"] else "") + "\n"
    if args[0] == "connect":
        return f"connected to {args[1]}\n"
    if args[0] == "exec-out":
        if args[1] == "screencap":
            return PNG
        return UI
    if args[0] != "shell":
        return ""
    line = " ".join(args[1:])
    state["log"].append(line)
    w = shlex.split(line)
    if w[0] == "getprop":
        return "".join(f"[{k}]: [{v}]\n" for k, v in state["props"].items())
    if w[:3] == ["pm", "list", "packages"]:
        p = state["packages"]
        names = p
        if "-d" in w:
            names = [k for k, v in p.items() if not v]
        elif "-e" in w:
            names = [k for k, v in p.items() if v]
        elif "-3" in w:
            names = state["third"]
        return "".join(f"package:{n}\n" for n in names)
    if w[:2] == ["pm", "disable-user"]:
        name = w[-1]
        if name in state["fail"] or name not in state["packages"]:
            return f"Exception: Unknown package: {name}\n"
        state["packages"][name] = False
        return f"Package {name} new state: disabled-user\n"
    if w[:2] == ["pm", "enable"]:
        state["packages"][w[-1]] = True
        return f"Package {w[-1]} new state: enabled\n"
    if w[0] == "settings" and w[1] == "get":
        return state["settings"].get(w[3], "null") + "\n"
    if w[0] == "settings" and w[1] == "put":
        state["settings"][w[3]] = w[4]
        return ""
    if w[0] == "settings" and w[1] == "delete":
        state["settings"].pop(w[3], None)
        return ""
    if w[0] == "df":
        return "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/data 12000000 9000000 3000000 75% /data\n"
    if w[:2] == ["cat", "/proc/meminfo"]:
        return "MemTotal: 2000000 kB\nMemFree: 200000 kB\nMemAvailable: 800000 kB\n"
    if w[0] == "cmd" and "resolve-activity" in w:
        return "priority=0 preferredOrder=0\ncom.google.android.apps.tv.launcherx/.MainActivity\n"
    return ""


if __name__ == "__main__":
    main()
