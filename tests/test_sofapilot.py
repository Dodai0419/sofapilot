import base64, importlib.util, json, os, pathlib, subprocess, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sofapilot as s

FAKE = ROOT / "examples" / "fake_adb.py"
_spec = importlib.util.spec_from_file_location("fake_adb", FAKE)
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)
ADS = ("com.google.android.feedback", "com.google.android.partnersetup", "com.google.android.apps.tv.dreamx")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)
        self.state_file = self.d / "tv.json"
        self.env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        self.env.update(FAKE_TV=str(self.state_file), SOFAPILOT_ADB=json.dumps([sys.executable, str(FAKE)]), SOFAPILOT_HOME=str(self.d / "home"))
        self.state_file.write_text(json.dumps(fake.DEFAULT))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, **kw):
        return subprocess.run([sys.executable, str(ROOT / "sofapilot.py"), *map(str, args)], capture_output=True, encoding="utf-8", errors="replace", env=self.env, **kw)

    def state(self):
        return json.loads(self.state_file.read_text())

    def set_state(self, **kw):
        st = self.state()
        st.update(kw)
        self.state_file.write_text(json.dumps(st))

    def enabled(self):
        return {k for k, v in self.state()["packages"].items() if v}


class Lists(unittest.TestCase):
    def test_bundle_has_every_upstream_device(self):
        data = s.load_lists()
        self.assertEqual(len(data["devices"]), 427)
        self.assertGreater(sum(len(g["packages"]) for d in data["devices"] for g in d["groups"]), 11000)

    def test_exact_match_rule_wins(self):
        d, how = s.match_list(s.load_lists(), {"manufacturer": "TPV", "model": "Philips Google TV TA1", "device": "PH1M_WW_9972"})
        self.assertEqual(how, "exact")
        self.assertEqual(d["id"], "philips-55oled708")

    def test_model_number_in_list_name_matches(self):
        d, how = s.match_list(s.load_lists(), {"manufacturer": "Acer", "model": "AR3 R3", "device": "x"})
        self.assertEqual(how, "model")
        self.assertEqual(d["id"], "acer-ar3-r3")

    def test_unknown_tv_has_no_match(self):
        self.assertEqual(s.match_list(s.load_lists(), {"manufacturer": "Nope", "model": "ZZ", "device": "y"}), (None, None))

    def test_search_by_words(self):
        self.assertTrue(all("philips" in d["name"].lower() for d in s.search_lists(s.load_lists(), "philips")))


class Safety(unittest.TestCase):
    def test_package_names_are_checked(self):
        for bad in ("a;reboot", "com.x y", "$(reboot)", "../x", "", "com.x\nreboot"):
            with self.assertRaises(s.Refused):
                s.check_package(bad)
        self.assertEqual(s.check_package("com.good.app_1"), "com.good.app_1")

    def test_protected_cannot_be_removed_by_any_list(self):
        for p in ("com.android.systemui", "com.android.settings", "com.google.android.gms", "com.android.vending", "com.google.android.webview", "org.droidtv.settings"):
            self.assertTrue(s.is_protected(p), p)

    def test_text_is_escaped_for_the_device_shell(self):
        quoted = s.input_text_arg("a b;reboot $(x) 'q'")
        import shlex
        self.assertEqual(len(shlex.split(quoted)), 1)
        self.assertEqual(shlex.split(quoted)[0], "a%sb;reboot%s$(x)%s'q'")


class Flow(Base):
    def test_status_reads_the_tv(self):
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        for part in ("Philips Google TV TA1", "Android 12", "philips-55oled708"):
            self.assertIn(part, r.stdout)

    def test_no_tv_explains_how_to_connect(self):
        self.set_state(online=False)
        r = self.cli("status")
        self.assertEqual(r.returncode, 2)
        self.assertIn("connect", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_adb_is_a_clear_refusal(self):
        self.env["SOFAPILOT_ADB"] = json.dumps([str(self.d / "no-adb")])
        r = self.cli("status")
        self.assertEqual(r.returncode, 2)
        self.assertIn("adb", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_plan_changes_nothing(self):
        before = self.enabled()
        r = self.cli("plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.enabled(), before)

    def test_apply_without_yes_changes_nothing(self):
        before = self.enabled()
        r = self.cli("apply", "--only", ",".join(ADS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--yes", r.stdout)
        self.assertEqual(self.enabled(), before)

    def test_apply_then_undo_restores_exactly(self):
        before = self.enabled()
        r = self.cli("apply", "--only", ",".join(ADS), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.enabled() & set(ADS))
        self.assertIn("disable-user --user 0 com.google.android.feedback", " ".join(self.state()["log"]))
        self.assertEqual(self.cli("undo", "--all").returncode, 0)
        self.assertEqual(self.enabled(), before)

    def test_undo_leaves_apps_that_were_already_off(self):
        self.cli("apply", "--only", "com.old.already.off,com.google.android.feedback", "--yes")
        self.cli("undo", "--all")
        st = self.state()["packages"]
        self.assertFalse(st["com.old.already.off"])
        self.assertTrue(st["com.google.android.feedback"])

    def test_undo_last_only_reverts_the_last_batch(self):
        self.cli("apply", "--only", ADS[0], "--yes")
        self.cli("apply", "--only", ADS[1], "--yes")
        self.cli("undo")
        st = self.state()["packages"]
        self.assertTrue(st[ADS[1]])
        self.assertFalse(st[ADS[0]])

    def test_undo_with_nothing_to_do_is_ok(self):
        r = self.cli("undo")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Nothing to undo", r.stdout)

    def test_protected_launcher_keep_and_uninstalled_are_never_touched(self):
        for p in ("com.android.systemui", "com.google.android.apps.tv.launcherx", "org.droidtv.settings"):
            r = self.cli("apply", "--only", p, "--yes")
            self.assertEqual(r.returncode, 2, p)
            self.assertTrue(self.state()["packages"][p])
        self.assertNotIn("uninstall", " ".join(self.state()["log"]))

    def test_apps_the_matched_list_says_to_keep_stay_on(self):
        lists = s.load_lists()
        keeper = next(d for d in lists["devices"] if d["id"] == "philips-55oled708")
        keeper["keep"] = [{"pkg": "com.netflix.ninja"}]
        tv = type("T", (), {"home_app": lambda self: None, "facts": lambda self: fake.DEFAULT and {"manufacturer": "TPV", "model": "Philips Google TV TA1", "device": "PH1M_WW_9972"}})()
        original = s.load_lists
        s.load_lists = lambda: lists
        try:
            self.assertIn("keep", s.protected_reason(tv, "com.netflix.ninja"))
        finally:
            s.load_lists = original

    def test_apply_stops_on_bad_package_name_before_any_change(self):
        r = self.cli("apply", "--only", f"{ADS[0]},a;reboot", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(self.state()["packages"][ADS[0]])
        self.assertNotIn("reboot", " ".join(self.state()["log"]))

    def test_partial_failure_is_reported_and_journal_still_correct(self):
        self.set_state(fail=[ADS[1]])
        r = self.cli("apply", "--only", ",".join(ADS[:3]), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn(ADS[1], r.stdout)
        self.cli("undo", "--all")
        self.assertTrue(self.state()["packages"][ADS[0]])

    def test_plan_list_follows_the_upstream_tick_rules(self):
        p = json.loads(self.cli("plan", "--json").stdout)
        self.assertEqual(p["list"], "philips-55oled708")
        self.assertTrue([i for i in p["items"] if i["ticked"]])
        self.assertNotIn("org.droidtv.demome", [i["pkg"] for i in p["items"] if i["ticked"]])
        for i in p["items"]:
            self.assertFalse(s.is_protected(i["pkg"]))
        self.assertTrue(all(not i["ticked"] for i in p["items"] if i.get("optional")))

    def test_other_list_is_never_ticked(self):
        p = json.loads(self.cli("plan", "--list", "aiwa-ai-pont-sa", "--json").stdout)
        self.assertEqual(p["how"], "picked")
        self.assertTrue(p["items"])
        self.assertTrue(all(not i["ticked"] for i in p["items"]))

    def test_unknown_list_is_refused(self):
        self.assertEqual(self.cli("plan", "--list", "nope").returncode, 2)

    def test_dns_on_off_and_undo_restores_value(self):
        self.assertEqual(self.cli("dns", "on", "--yes").returncode, 0)
        st = self.state()["settings"]
        self.assertEqual(st["private_dns_mode"], "hostname")
        self.assertEqual(st["private_dns_specifier"], "dns.adguard-dns.com")
        self.cli("undo")
        self.assertEqual(self.state()["settings"]["private_dns_mode"], "off")

    def test_dns_host_is_validated(self):
        self.assertEqual(self.cli("dns", "on", "--host", "a;reboot", "--yes").returncode, 2)

    def test_animations_and_undo(self):
        self.cli("animations", "0.5", "--yes")
        self.assertEqual(self.state()["settings"]["window_animation_scale"], "0.5")
        self.cli("undo")
        self.assertEqual(self.state()["settings"]["window_animation_scale"], "1.0")
        self.assertEqual(self.cli("animations", "abc", "--yes").returncode, 2)

    def test_history_shows_changes(self):
        self.cli("apply", "--only", ADS[0], "--yes")
        r = self.cli("history")
        self.assertIn(ADS[0], r.stdout)

    def test_journals_are_per_tv(self):
        self.cli("apply", "--only", ADS[0], "--yes")
        st = self.state()
        st["serial"] = "10.0.0.9:5555"
        self.state_file.write_text(json.dumps(st))
        self.assertIn("Nothing to undo", self.cli("undo").stdout)

    def test_remote_controls(self):
        for args, expect in ((("key", "home"), "input keyevent KEYCODE_HOME"), (("key", "volume-up"), "input keyevent KEYCODE_VOLUME_UP"), (("tap", 10, 20), "input tap 10 20"), (("swipe", 1, 2, 3, 4), "input swipe 1 2 3 4"), (("open", "com.netflix.ninja"), "com.netflix.ninja")):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(expect, " ".join(self.state()["log"]))
        self.assertEqual(self.cli("key", "reboot").returncode, 2)
        self.assertEqual(self.cli("tap", "1;reboot", 2).returncode, 2)

    def test_type_is_escaped(self):
        self.assertEqual(self.cli("type", "hello there; reboot").returncode, 0)
        log = " ".join(self.state()["log"])
        self.assertIn("input text 'hello%sthere;%sreboot'", log)

    def test_screenshot_and_elements(self):
        out = self.d / "shot.png"
        self.assertEqual(self.cli("screenshot", "-o", out).returncode, 0)
        self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        r = self.cli("elements")
        self.assertIn("Netflix", r.stdout)
        self.assertIn("200", r.stdout)

    def test_two_tvs_need_a_serial(self):
        self.assertEqual(s.pick_serial("List of devices attached\na\tdevice\nb\tdevice\n", "b"), "b")
        with self.assertRaises(s.Refused):
            s.pick_serial("List of devices attached\na\tdevice\nb\tdevice\n", None)
        with self.assertRaises(s.Refused):
            s.pick_serial("List of devices attached\na\tunauthorized\n", None)

    def test_card(self):
        svg = self.d / "c.svg"
        self.assertEqual(self.cli("plan", "--card", svg).returncode, 0)
        self.assertIn("<svg", svg.read_text(encoding="utf-8"))

    def test_demo(self):
        self.env.pop("SOFAPILOT_ADB"); self.env.pop("FAKE_TV"); self.env.pop("SOFAPILOT_HOME")
        r = self.cli("--demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restored", r.stdout)


class Mcp(Base):
    def rpc(self, *messages):
        text = "".join(json.dumps(m) + "\n" for m in messages)
        r = self.cli("mcp", input=text)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]

    def call(self, name, **args):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return out[0]["result"]

    def test_handshake_and_tools(self):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual([m.get("id") for m in out], [1, 2, 3])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in out[1]["result"]["tools"]}
        self.assertLessEqual({"tv_status", "tv_plan", "tv_disable", "tv_undo", "tv_screenshot", "tv_elements", "tv_key", "tv_type", "tv_tap", "tv_swipe", "tv_open"}, names)
        self.assertEqual(out[2]["error"]["code"], -32601)

    def test_disable_and_undo_through_mcp(self):
        r = self.call("tv_disable", packages=[ADS[0]])
        self.assertNotIn("isError", r)
        self.assertFalse(self.state()["packages"][ADS[0]])
        self.call("tv_undo")
        self.assertTrue(self.state()["packages"][ADS[0]])

    def test_protected_comes_back_as_tool_error(self):
        r = self.call("tv_disable", packages=["com.android.systemui"])
        self.assertTrue(r["isError"])
        self.assertTrue(self.state()["packages"]["com.android.systemui"])

    def test_screenshot_is_an_image(self):
        r = self.call("tv_screenshot")
        self.assertEqual(r["content"][0]["type"], "image")
        self.assertEqual(base64.b64decode(r["content"][0]["data"])[:4], b"\x89PNG")

    def test_garbage_input_does_not_kill_the_server(self):
        r = self.cli("mcp", input='not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["id"], 9)

    def test_no_shell_tool_exists(self):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertFalse([t for t in out[0]["result"]["tools"] if "shell" in t["name"] or "uninstall" in t["name"]])


if __name__ == "__main__":
    unittest.main()
