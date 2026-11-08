#!/usr/bin/env python3
"""Let Claude or Codex clean up any Android TV over Wi-Fi, with undo for every change.

Talks to the TV with adb. Turns apps off, never uninstalls them. Writes every change to a journal first.
"""
import argparse, base64, gzip, html, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent
PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
DEFAULT_DNS = "dns.adguard-dns.com"
ANIMATIONS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
KEYS = {"home": "HOME", "back": "BACK", "menu": "MENU", "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT", "ok": "DPAD_CENTER", "play": "MEDIA_PLAY_PAUSE", "mute": "VOLUME_MUTE", "volume-up": "VOLUME_UP", "volume-down": "VOLUME_DOWN", "power": "POWER"}
# Apps a TV needs to boot, update, stay online, show web pages, show its home screen or take input.
PROTECTED = frozenset("""
android com.android.systemui com.android.settings com.android.tv.settings com.google.android.tv.settings com.android.shell com.android.bluetooth
com.android.providers.settings com.android.providers.tv com.android.providers.media com.android.providers.downloads com.android.providers.contacts
com.android.packageinstaller com.google.android.packageinstaller com.android.permissioncontroller com.google.android.permissioncontroller
com.google.android.gms com.google.android.gsf com.android.vending com.google.android.webview com.android.webview com.android.chrome.webview
com.android.inputmethod.latin com.google.android.inputmethod.latin com.google.android.tv.remote.service com.android.tv.remote.service
com.google.android.tungsten.setupwraith com.android.networkstack com.google.android.networkstack com.android.networkstack.tethering com.android.wifi.resources
com.android.externalstorage com.android.keychain com.android.location.fused com.android.server.telecom com.android.se
com.amazon.tv.launcher com.amazon.tv.settings.v2 com.amazon.device.software.ota com.amazon.firetv.youtube.tv
""".split())


class Refused(Exception):
    pass


def extra_protected():
    try:
        data = json.loads((ROOT / "data" / "protected.json").read_text(encoding="utf-8"))
        return {p for g in data["groups"] for p in g["packages"]}
    except (OSError, ValueError, KeyError):
        return set()


PROTECTED_ALL = PROTECTED | extra_protected()


def is_protected(pkg):
    return pkg in PROTECTED_ALL or pkg.startswith("com.android.providers.")


def check_package(name):
    if not PACKAGE.match(name or "") or "\n" in name:
        raise Refused(f"'{name}' is not a package name")
    return name


def input_text_arg(text):
    """One quoted argument for 'input text'. Spaces become %s, which is how that command spells them."""
    return shlex.quote(text.replace(" ", "%s"))


def load_lists():
    return json.loads(gzip.decompress((ROOT / "data" / "tvlists.json.gz").read_bytes()))


def normal(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def match_list(data, tv):
    for d in data["devices"]:
        rule = d.get("match")
        if rule and all(tv.get(k) in v for k, v in rule.items() if isinstance(v, list) and k in ("manufacturer", "model", "device")):
            return d, "exact"
    model, maker = normal(tv.get("model")), normal(tv.get("manufacturer"))
    if len(model) >= 4:
        for d in data["devices"]:
            name, ident = normal(d["name"]), normal(d["id"])
            if (model in name or model in ident) and (not maker or maker in name or maker in ident):
                return d, "model"
    return None, None


def search_lists(data, words):
    want = normal(words)
    return [d for d in data["devices"] if want in normal(d["name"]) or want in normal(d["id"])]


class Tv:
    def __init__(self, serial=None):
        raw = os.environ.get("SOFAPILOT_ADB")
        self.adb = json.loads(raw) if raw else [shutil.which("adb") or "adb"]
        self.serial = pick_serial(self.run_text("devices"), serial)

    def run(self, *args, timeout=60):
        prefix = list(self.adb) + (["-s", self.serial] if getattr(self, "serial", None) else [])
        try:
            p = subprocess.run(prefix + list(args), capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise Refused("adb was not found. Install Android platform-tools and try again")
        except subprocess.TimeoutExpired:
            raise Refused("the TV did not answer in time. Check that it is on and on the same network")
        if p.returncode:
            raise Refused(f"adb failed: {p.stderr.decode('utf-8', 'replace').strip()[:200] or p.returncode}")
        return p.stdout

    def run_text(self, *args):
        return self.run(*args).decode("utf-8", "replace")

    def shell(self, line):
        return self.run_text("shell", line).strip()

    def facts(self):
        found = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]\s*$", self.shell("getprop"), re.M))
        return {k: found.get(f"ro.{v}", "") for k, v in dict(manufacturer="product.manufacturer", model="product.model", device="product.device", sdk="build.version.sdk", android="build.version.release").items()}

    def packages(self, flag=""):
        out = self.shell(f"pm list packages {flag}".strip())
        return {line[8:].strip() for line in out.splitlines() if line.startswith("package:")}

    def home_app(self):
        out = self.shell("cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME")
        for line in reversed(out.splitlines()):
            if "/" in line:
                return line.split("/")[0].strip()
        return None

    def setting(self, key):
        return self.shell(f"settings get global {key}")

    def put_setting(self, key, value):
        if value in ("null", "", None):
            self.shell(f"settings delete global {key}")
        else:
            self.shell(f"settings put global {key} {shlex.quote(str(value))}")


def pick_serial(listing, wanted):
    ready, other = [], []
    for line in listing.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            (ready if parts[1] == "device" else other).append((parts[0], parts[1]))
    names = [r[0] for r in ready]
    if wanted:
        if wanted in names:
            return wanted
        raise Refused(f"{wanted} is not connected. Connected: {', '.join(names) or 'none'}")
    if len(names) == 1:
        return names[0]
    if names:
        raise Refused(f"{len(names)} TVs are connected. Pick one with --serial: {', '.join(names)}")
    if other:
        raise Refused(f"{other[0][0]} is {other[0][1]}. On the TV, tick Always allow and press Allow, then try again")
    raise Refused("no TV found. Turn on network debugging on the TV, then run: sofapilot connect <TV-IP>")


def home():
    return pathlib.Path(os.environ.get("SOFAPILOT_HOME") or pathlib.Path.home() / ".sofapilot")


class Journal:
    """What was off before we arrived, and each change, per TV. Saved before and after every change."""

    def __init__(self, serial):
        self.path = home() / (re.sub(r"[^\w.-]", "_", serial) + ".json")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = dict(baseline_off=None, changes=[])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def begin(self, tv, kind, **fields):
        if self.data["baseline_off"] is None:
            self.data["baseline_off"] = sorted(tv.packages("-d"))
        change = dict(when=time.strftime("%Y-%m-%d %H:%M:%S"), kind=kind, undone=False, **fields)
        self.data["changes"].append(change)
        self.save()
        return change


def build_plan(tv, list_id=None):
    data, facts = load_lists(), tv.facts()
    if list_id:
        chosen = next((d for d in data["devices"] if d["id"] == list_id), None)
        if not chosen:
            raise Refused(f"no list named {list_id}. Find one with: sofapilot lists <words>")
        how = "picked"
    else:
        chosen, how = match_list(data, facts)
    installed, enabled = tv.packages(), tv.packages("-e")
    launcher = tv.home_app()
    keep = {k["pkg"] for k in (chosen or {}).get("keep", [])} | ({launcher} if launcher else set()) | ({(chosen or {}).get("launcher")} - {None})
    same_sdk = bool(chosen) and str(chosen.get("sdk")) == facts["sdk"]
    items = []
    for group in (chosen or {}).get("groups", []):
        for p in group["packages"]:
            pkg = p["pkg"]
            if pkg not in installed or pkg not in enabled or pkg in keep or is_protected(pkg) or not PACKAGE.match(pkg):
                continue
            optional = bool(p.get("optional"))
            items.append(dict(pkg=pkg, group=group["title"], note=p.get("note", ""), optional=optional, ticked=how in ("exact", "model") and same_sdk and not optional))
    return dict(tv=facts, list=(chosen or {}).get("id"), list_name=(chosen or {}).get("name"), how=how, same_sdk=same_sdk, items=items, enabled=len(enabled), source=data["source"])


def protected_reason(tv, pkg):
    if is_protected(pkg):
        return "the TV needs it"
    if pkg == tv.home_app():
        return "it is the TV's home screen"
    chosen, _ = match_list(load_lists(), tv.facts())
    if chosen and pkg in {k["pkg"] for k in chosen.get("keep", [])}:
        return "this TV's list says to keep it"
    return None


def disable(tv, packages):
    """Turn apps off. Checks every name first, so one bad name changes nothing."""
    packages = [check_package(p) for p in packages]
    for pkg in packages:
        why = protected_reason(tv, pkg)
        if why:
            raise Refused(f"{pkg} stays on: {why}")
    journal = Journal(tv.serial)
    change = journal.begin(tv, "disable", packages=[])
    enabled = tv.packages("-e")
    done, failed, skipped = [], [], []
    for pkg in packages:
        if pkg not in enabled:
            skipped.append(pkg)
            continue
        out = tv.shell(f"pm disable-user --user 0 {pkg}")
        if "disabled" in out.lower():
            done.append(pkg)
            change["packages"] = done
            journal.save()
        else:
            failed.append(f"{pkg}: {out[:120]}")
    return done, failed, skipped


def set_settings(tv, kind, values):
    journal = Journal(tv.serial)
    before = {k: tv.setting(k) for k in values}
    journal.begin(tv, kind, before=before, after=values)
    for k, v in values.items():
        tv.put_setting(k, v)


def undo(tv, everything=False):
    journal = Journal(tv.serial)
    todo = [c for c in journal.data["changes"] if not c["undone"] and (c.get("packages") or c.get("before"))]
    if not everything:
        todo = todo[-1:]
    lines = []
    keep_off = set(journal.data["baseline_off"] or [])
    for change in reversed(todo):
        if change["kind"] == "disable":
            for pkg in change["packages"]:
                if pkg not in keep_off:
                    tv.shell(f"pm enable {check_package(pkg)}")
                    lines.append(f"on again: {pkg}")
        else:
            for k, v in change["before"].items():
                tv.put_setting(k, v)
                lines.append(f"restored: {k} = {v}")
        change["undone"] = True
        journal.save()
    return lines


def history_lines(tv):
    lines = []
    for c in Journal(tv.serial).data["changes"]:
        state = "undone" if c["undone"] else "active"
        if c["kind"] == "disable":
            if c["packages"]:
                lines.append(f"{c['when']}  [{state}] turned off: {', '.join(c['packages'])}")
        else:
            lines.append(f"{c['when']}  [{state}] {c['kind']}: {c['after']}")
    return lines


def press(tv, key):
    if key not in KEYS:
        raise Refused(f"unknown key '{key}'. Use one of: {', '.join(KEYS)}")
    tv.shell(f"input keyevent KEYCODE_{KEYS[key]}")


def whole(value):
    try:
        n = int(str(value))
    except ValueError:
        raise Refused(f"'{value}' is not a whole number")
    if not 0 <= n <= 20000:
        raise Refused(f"{value} is off the screen")
    return n


def type_text(tv, text):
    if not text or "\n" in text or len(text) > 500:
        raise Refused("type one line of text, up to 500 characters")
    tv.shell(f"input text {input_text_arg(text)}")


def open_app(tv, pkg):
    tv.shell(f"monkey -p {check_package(pkg)} -c android.intent.category.LEANBACK_LAUNCHER 1")


def elements(tv):
    raw = tv.run_text("exec-out", "uiautomator", "dump", "/dev/tty")
    start, end = raw.find("<?xml"), raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise Refused("the TV did not return its screen layout. Some video apps block this")
    try:
        root = ET.fromstring(raw[start:end + len("</hierarchy>")])
    except ET.ParseError as e:
        raise Refused(f"the TV's screen layout could not be read: {e}")
    out = []
    for n in root.iter("node"):
        text, desc = n.get("text", ""), n.get("content-desc", "")
        box = [int(v) for v in re.findall(r"\d+", n.get("bounds", ""))]
        if (text or desc or n.get("clickable") == "true") and len(box) == 4:
            out.append(dict(text=text, description=desc, id=n.get("resource-id", ""), x=(box[0] + box[2]) // 2, y=(box[1] + box[3]) // 2))
    return out


def screenshot(tv):
    data = tv.run("exec-out", "screencap", "-p")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise Refused("the TV did not return a picture. Some video apps block screenshots")
    return data


def status_text(tv):
    f = tv.facts()
    data = load_lists()
    chosen, how = match_list(data, f)
    df = tv.shell("df /data").splitlines()
    free = f"{int(df[-1].split()[3]) // 1024:,} MB" if len(df) > 1 and len(df[-1].split()) > 3 and df[-1].split()[3].isdigit() else "unknown"
    mem = re.search(r"MemAvailable:\s+(\d+)", tv.shell("cat /proc/meminfo"))
    active = [c for c in Journal(tv.serial).data["changes"] if not c["undone"] and (c.get("packages") or c.get("before"))]
    return "\n".join([
        f"{f['manufacturer']} {f['model']} ({tv.serial})",
        f"Android {f['android']}, SDK {f['sdk']}",
        f"Free storage: {free}. Free memory: {int(mem.group(1)) // 1024 if mem else 0:,} MB",
        f"Apps on: {len(tv.packages('-e'))}, off: {len(tv.packages('-d'))}",
        f"Private DNS: {tv.setting('private_dns_mode')}",
        f"Debloat list: {chosen['id']} ({how} match)" if chosen else "Debloat list: none matched. Find one with: sofapilot lists <words>",
        f"Changes that can be undone: {len(active)}",
    ])


def plan_text(p):
    lines = []
    if not p["list"]:
        return f"No list matches {p['tv']['manufacturer']} {p['tv']['model']}. Find one with: sofapilot lists <words>, then: sofapilot plan --list ID"
    ticked = [i for i in p["items"] if i["ticked"]]
    lines.append(f"{p['tv']['manufacturer']} {p['tv']['model']}: list {p['list']} ({p['how']} match)")
    if p["how"] == "picked" or not p["same_sdk"]:
        lines.append("This list was not tested on this exact model and Android version, so nothing is ticked. Choose apps yourself with --only.")
    group = None
    for i in p["items"]:
        if i["group"] != group:
            group = i["group"]
            lines.append(f"\n{group}")
        note = f"  ({i['note']})" if i["note"] else ""
        lines.append(f"  [{'x' if i['ticked'] else ' '}] {i['pkg']}{note}")
    lines.append(f"\n{len(ticked)} of {len(p['items'])} apps ticked. {p['enabled']} apps are on now. Nothing has changed.")
    return "\n".join(lines)


def card(p):
    e = html.escape
    ticked = sum(1 for i in p["items"] if i["ticked"])
    rows = [i for i in p["items"] if i["ticked"]][:6] or p["items"][:6]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400" viewBox="0 0 720 400" font-family="Segoe UI,Helvetica,Arial,sans-serif">', '<rect width="720" height="400" rx="14" fill="#10141c"/>', f'<text x="36" y="56" font-size="24" font-weight="700" fill="#f2f4f8">{e(p["tv"]["manufacturer"] + " " + p["tv"]["model"])[:50]}</text>', f'<text x="36" y="84" font-size="14" fill="#9aa3b2">Android {e(p["tv"]["android"])}, list {e(str(p["list"]))}</text>', f'<text x="36" y="146" font-size="52" font-weight="700" fill="#30a46c">{ticked}</text>', '<text x="36" y="172" font-size="14" fill="#9aa3b2">apps to turn off</text>', f'<text x="236" y="146" font-size="52" font-weight="700" fill="#5b8def">{p["enabled"]}</text>', '<text x="236" y="172" font-size="14" fill="#9aa3b2">apps on now</text>', '<text x="436" y="146" font-size="52" font-weight="700" fill="#f5a524">1</text>', '<text x="436" y="172" font-size="14" fill="#9aa3b2">command to undo it all</text>']
    for k, i in enumerate(rows):
        parts.append(f'<text x="36" y="{212 + k * 25}" font-size="14" fill="#d5d9e2">{e(i["pkg"])}</text>')
        parts.append(f'<text x="360" y="{212 + k * 25}" font-size="13" fill="#7c8596">{e(i["group"][:50])}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


TOOLS = {
    "tv_status": ("Read the TV's model, Android version, free storage and memory, app counts and matching debloat list.", {}, []),
    "tv_plan": ("List apps the matching debloat list would turn off. Changes nothing. Show this to the person before tv_disable.", {"list": {"type": "string", "description": "A list id from tv_lists. Leave out to use the matching list."}}, []),
    "tv_lists": ("Search the 427 bundled debloat lists by brand or model.", {"words": {"type": "string"}}, ["words"]),
    "tv_apps": ("List the apps on the TV: on, off and those the owner installed.", {}, []),
    "tv_disable": ("Turn apps off with pm disable-user and record it for undo. Apps the TV needs are refused. Ask the person first.", {"packages": {"type": "array", "items": {"type": "string"}}}, ["packages"]),
    "tv_undo": ("Undo the last change, or every change with all=true. Apps that were off before stay off.", {"all": {"type": "boolean"}}, []),
    "tv_history": ("List every recorded change on this TV.", {}, []),
    "tv_private_dns": ("Turn ad-blocking Private DNS on or off. It can be undone.", {"on": {"type": "boolean"}, "host": {"type": "string"}}, ["on"]),
    "tv_animations": ("Set animation speed, from 0 to 1. Lower feels faster. It can be undone.", {"scale": {"type": "number"}}, ["scale"]),
    "tv_screenshot": ("See the TV's screen, as a PNG.", {}, []),
    "tv_elements": ("List text and buttons on the screen, with coordinates for tv_tap.", {}, []),
    "tv_key": ("Press a remote button.", {"key": {"type": "string", "enum": list(KEYS)}}, ["key"]),
    "tv_type": ("Type text into the focused field.", {"text": {"type": "string"}}, ["text"]),
    "tv_tap": ("Tap a point on the screen.", {"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]),
    "tv_swipe": ("Swipe between two points.", {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"}}, ["x1", "y1", "x2", "y2"]),
    "tv_open": ("Open an installed app by package name.", {"package": {"type": "string"}}, ["package"]),
}


def set_dns(tv, on, host=None):
    host = host or DEFAULT_DNS
    if on and not HOST.match(host):
        raise Refused(f"'{host}' is not a host name")
    set_settings(tv, "dns", dict(private_dns_mode="hostname", private_dns_specifier=host) if on else dict(private_dns_mode="off"))


def set_animations(tv, scale):
    try:
        value = float(scale)
    except (TypeError, ValueError):
        raise Refused(f"'{scale}' is not a number from 0 to 1")
    if not 0 <= value <= 1:
        raise Refused(f"'{scale}' is not a number from 0 to 1")
    set_settings(tv, "animations", {k: str(value) for k in ANIMATIONS})


def call_tool(name, a, serial):
    tv = Tv(serial)
    text = lambda t: {"content": [{"type": "text", "text": t}]}
    if name == "tv_status":
        return text(status_text(tv))
    if name == "tv_plan":
        return text(json.dumps(build_plan(tv, a.get("list")), ensure_ascii=False))
    if name == "tv_lists":
        return text("\n".join(f"{d['id']}  {d['name']}" for d in search_lists(load_lists(), a.get("words", ""))[:40]) or "No list found")
    if name == "tv_apps":
        return text(json.dumps(dict(on=sorted(tv.packages("-e")), off=sorted(tv.packages("-d")), installed_by_owner=sorted(tv.packages("-3")))))
    if name == "tv_disable":
        done, failed, skipped = disable(tv, a.get("packages") or [])
        return dict(text(json.dumps(dict(turned_off=done, failed=failed, already_off=skipped))), **({"isError": True} if failed else {}))
    if name == "tv_undo":
        return text("\n".join(undo(tv, bool(a.get("all")))) or "Nothing to undo")
    if name == "tv_history":
        return text("\n".join(history_lines(tv)) or "No changes recorded")
    if name == "tv_private_dns":
        set_dns(tv, bool(a.get("on")), a.get("host"))
        return text("Done. tv_undo reverts it.")
    if name == "tv_animations":
        set_animations(tv, a.get("scale"))
        return text("Done. tv_undo reverts it.")
    if name == "tv_screenshot":
        return {"content": [{"type": "image", "data": base64.b64encode(screenshot(tv)).decode(), "mimeType": "image/png"}]}
    if name == "tv_elements":
        return text(json.dumps(elements(tv), ensure_ascii=False))
    if name == "tv_key":
        press(tv, a.get("key"))
    elif name == "tv_type":
        type_text(tv, a.get("text"))
    elif name == "tv_tap":
        tv.shell(f"input tap {whole(a.get('x'))} {whole(a.get('y'))}")
    elif name == "tv_swipe":
        tv.shell("input swipe " + " ".join(str(whole(a.get(k))) for k in ("x1", "y1", "x2", "y2")))
    elif name == "tv_open":
        open_app(tv, a.get("package"))
    else:
        raise Refused(f"unknown tool {name}")
    return text("Done")


def serve_mcp(serial):
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        except (ValueError, AttributeError):
            print(json.dumps(dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="parse error"))), flush=True)
            continue
        if mid is None:
            continue
        if method == "initialize":
            res = dict(protocolVersion=params.get("protocolVersion", "2025-06-18"), capabilities=dict(tools={}), serverInfo=dict(name="sofapilot", version="0.1.0"))
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = dict(tools=[dict(name=n, description=d, inputSchema=dict(type="object", properties=p, required=r)) for n, (d, p, r) in TOOLS.items()])
        elif method == "tools/call":
            try:
                res = call_tool(params.get("name"), params.get("arguments") or {}, serial)
            except Refused as e:
                res = dict(content=[dict(type="text", text=str(e))], isError=True)
        else:
            print(json.dumps(dict(jsonrpc="2.0", id=mid, error=dict(code=-32601, message=f"unknown method {method}"))), flush=True)
            continue
        print(json.dumps(dict(jsonrpc="2.0", id=mid, result=res)), flush=True)


def demo():
    """Run the whole job on a pretend TV, so nothing here needs real hardware."""
    with tempfile.TemporaryDirectory() as d:
        os.environ.update(FAKE_TV=str(pathlib.Path(d) / "tv.json"), SOFAPILOT_HOME=str(pathlib.Path(d) / "home"), SOFAPILOT_ADB=json.dumps([sys.executable, str(ROOT / "examples" / "fake_adb.py")]))
        tv = Tv()
        print("This is a pretend TV, not real hardware.\n")
        print(status_text(tv), "\n")
        plan = build_plan(tv)
        print(plan_text(plan), "\n")
        pick = [i["pkg"] for i in plan["items"] if i["ticked"]]
        before = tv.packages("-e")
        done, _, _ = disable(tv, pick)
        print(f"Turned off {len(done)} apps. Apps on: {len(before)} -> {len(tv.packages('-e'))}")
        undo(tv, True)
        print(f"After undo --all, apps on: {len(tv.packages('-e'))}. The TV is restored." if tv.packages("-e") == before else "UNDO FAILED")
        return 0 if tv.packages("-e") == before else 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="sofapilot", description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true", help="run the whole job on a pretend TV")
    ap.add_argument("--serial", help="which TV, when several are connected")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("connect", help="connect to a TV on your Wi-Fi"); c.add_argument("address")
    sub.add_parser("status", help="how the TV is doing")
    l = sub.add_parser("lists", help="search the debloat lists"); l.add_argument("words", nargs="+")
    p = sub.add_parser("plan", help="show what would be turned off. Changes nothing")
    p.add_argument("--list"); p.add_argument("--json", action="store_true"); p.add_argument("--card", metavar="FILE.svg")
    a = sub.add_parser("apply", help="turn apps off")
    a.add_argument("--only", help="comma-separated package names, instead of the ticked plan"); a.add_argument("--list"); a.add_argument("--yes", action="store_true", help="make the change")
    u = sub.add_parser("undo", help="undo the last change"); u.add_argument("--all", action="store_true")
    sub.add_parser("history", help="every recorded change")
    sub.add_parser("apps", help="list the apps on the TV")
    d = sub.add_parser("dns", help="ad-blocking Private DNS"); d.add_argument("state", choices=("on", "off")); d.add_argument("--host"); d.add_argument("--yes", action="store_true")
    n = sub.add_parser("animations", help="animation speed, 0 to 1"); n.add_argument("scale"); n.add_argument("--yes", action="store_true")
    s = sub.add_parser("screenshot", help="save the TV's screen"); s.add_argument("-o", "--out", default="tv.png")
    sub.add_parser("elements", help="list text and buttons on the screen")
    k = sub.add_parser("key", help="press a remote button"); k.add_argument("key")
    t = sub.add_parser("type", help="type text"); t.add_argument("text")
    q = sub.add_parser("tap", help="tap a point"); q.add_argument("x"); q.add_argument("y")
    w = sub.add_parser("swipe", help="swipe"); [w.add_argument(v) for v in ("x1", "y1", "x2", "y2")]
    o = sub.add_parser("open", help="open an app"); o.add_argument("package")
    sub.add_parser("mcp", help="run as an MCP server over stdio")
    args = ap.parse_args(argv)
    try:
        if args.demo:
            return demo()
        if args.cmd == "mcp":
            serve_mcp(args.serial)
            return 0
        if args.cmd == "lists":
            found = search_lists(load_lists(), " ".join(args.words))
            for x in found[:40]:
                print(f"{x['id']:<44}{x['name']}")
            print(f"{len(found)} lists found")
            return 0 if found else 1
        if not args.cmd:
            ap.print_help()
            return 2
        if args.cmd == "connect":
            if not re.fullmatch(r"[\w.:-]+", args.address):
                raise Refused(f"'{args.address}' is not an address")
            raw = os.environ.get("SOFAPILOT_ADB")
            cmd = (json.loads(raw) if raw else [shutil.which("adb") or "adb"]) + ["connect", args.address if ":" in args.address else args.address + ":5555"]
            try:
                print(subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip())
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                raise Refused(f"adb could not connect: {e}")
            return 0
        tv = Tv(args.serial)
        if args.cmd == "status":
            print(status_text(tv))
        elif args.cmd == "plan":
            plan = build_plan(tv, args.list)
            if args.card:
                pathlib.Path(args.card).write_text(card(plan), encoding="utf-8")
            print(json.dumps(plan, indent=1, ensure_ascii=False) if args.json else plan_text(plan))
        elif args.cmd == "apply":
            if args.only:
                names = [check_package(x.strip()) for x in args.only.split(",") if x.strip()]
                for name in names:
                    why = protected_reason(tv, name)
                    if why:
                        raise Refused(f"{name} stays on: {why}")
            else:
                names = [i["pkg"] for i in build_plan(tv, args.list)["items"] if i["ticked"]]
            if not names:
                print("Nothing to turn off. See: sofapilot plan")
                return 0
            if not args.yes:
                print("Would turn off:\n  " + "\n  ".join(names) + "\nNothing has changed. Add --yes to do it.")
                return 0
            done, failed, skipped = disable(tv, names)
            print(f"Turned off {len(done)} apps. Undo with: sofapilot undo")
            for x in skipped:
                print(f"already off: {x}")
            for x in failed:
                print(f"failed: {x}")
            return 1 if failed else 0
        elif args.cmd == "undo":
            lines = undo(tv, args.all)
            print("\n".join(lines) or "Nothing to undo")
        elif args.cmd == "history":
            print("\n".join(history_lines(tv)) or "No changes recorded")
        elif args.cmd == "apps":
            third = tv.packages("-3")
            off = tv.packages("-d")
            for x in sorted(tv.packages()):
                print(f"{'off' if x in off else 'on ':<4}{'mine' if x in third else '    ':<5}{x}")
        elif args.cmd in ("dns", "animations"):
            if not args.yes:
                print("Nothing has changed. Add --yes to do it.")
                return 0
            if args.cmd == "dns":
                set_dns(tv, args.state == "on", args.host)
            else:
                set_animations(tv, args.scale)
            print("Done. Undo with: sofapilot undo")
        elif args.cmd == "screenshot":
            pathlib.Path(args.out).write_bytes(screenshot(tv))
            print(f"wrote {args.out}")
        elif args.cmd == "elements":
            for x in elements(tv):
                print(f"({x['x']}, {x['y']})  {x['text'] or x['description'] or x['id']}")
        elif args.cmd == "key":
            press(tv, args.key)
        elif args.cmd == "type":
            type_text(tv, args.text)
        elif args.cmd == "tap":
            tv.shell(f"input tap {whole(args.x)} {whole(args.y)}")
        elif args.cmd == "swipe":
            tv.shell("input swipe " + " ".join(str(whole(v)) for v in (args.x1, args.y1, args.x2, args.y2)))
        elif args.cmd == "open":
            open_app(tv, args.package)
        return 0
    except Refused as e:
        print(f"sofapilot: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
