"""Nicknames, profiles and DDC error messages."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from screen_control.query_parser import Action, parse_query
from screen_control.results import profile_plan
from screen_control.store import DEFAULT_PROFILES, Store, data_dir
from screen_control.views import build_results
from screen_control.win32.ddc import VCP_CONTRAST, describe_error

from .test_logic import SCREENS, _screen


def titles(text: str, screens=SCREENS, profiles=None, customized=frozenset()) -> list[str]:  # noqa: ANN001
    return [(r.title_tooltip if r.progress_bar else r.title) or "" for r in build_results(parse_query(text), screens, "sc", profiles, customized)]


class NameTests(unittest.TestCase):
    def test_parse_keeps_case_and_screen(self) -> None:
        q = parse_query("name #2 My Gaming Desk")
        self.assertIs(q.action, Action.NAME)
        self.assertEqual(q.screen_number, 2)
        self.assertEqual(q.nickname, "My Gaming Desk")
        self.assertEqual(parse_query("#1 rename Work").nickname, "Work")
        self.assertEqual(parse_query("name #1").nickname, "")

    def test_numbers_inside_names_are_kept(self) -> None:
        self.assertEqual(parse_query("name #1 Desk 2").nickname, "Desk 2")

    def test_name_word_elsewhere_is_not_a_rename(self) -> None:
        self.assertIsNot(parse_query("lg name").action, Action.NAME)

    def test_views(self) -> None:
        self.assertEqual(titles("name")[0], "Rename 1 · FQ320")
        self.assertEqual(titles("name #2 Gaming"), ["Name screen 2 “Gaming”"])
        self.assertEqual(titles("name #2"), ["Type a name for 2 · LG ULTRAGEAR"])
        named = [replace(SCREENS[0], nickname="Work"), SCREENS[1]]
        self.assertIn("Remove the nickname “Work”", titles("name #1", named))
        self.assertIn("There is no screen #7", titles("name #7 X")[0])

    def test_nickname_in_title_and_search(self) -> None:
        s = replace(SCREENS[0], nickname="Work")
        self.assertEqual(s.title, "1 · Work (FQ320)")
        self.assertIn("work", s.search_text)
        self.assertEqual(titles("work 50", [s, SCREENS[1]]), ["Set brightness to 50% — 1 · Work (FQ320)"])


class ProfileTests(unittest.TestCase):
    def test_parse(self) -> None:
        q = parse_query("night")
        self.assertEqual((q.action, q.profile_name, q.profile_op), (Action.PROFILE, "night", "apply"))
        q = parse_query("night save")
        self.assertEqual((q.profile_name, q.profile_op), ("night", "save"))
        q = parse_query("profile save Gaming Setup")
        self.assertEqual((q.profile_name, q.profile_op), ("Gaming Setup", "save"))
        q = parse_query("profile")
        self.assertEqual((q.profile_name, q.profile_op), (None, "apply"))
        self.assertEqual(parse_query("profile reset night").profile_op, "delete")

    def test_plan_uses_screen_entry_then_default(self) -> None:
        profile = {"*": {"brightness": 10, "hdr": False}, SCREENS[1].stable_id: {"brightness": 30, "contrast": 50}}
        plan = profile_plan(profile, SCREENS)
        self.assertEqual(plan["path1"], {"brightness": 10, "hdr": False})
        self.assertEqual(plan["path2"], {"brightness": 30, "contrast": 50})

    def test_list_and_apply_views(self) -> None:
        t = titles("profile", profiles=DEFAULT_PROFILES)
        self.assertEqual(t[:3], ["Apply profile “day”", "Apply profile “night”", "Apply profile “movie”"])
        t = titles("night", profiles=DEFAULT_PROFILES)
        self.assertEqual(t[0], "Apply profile “night”")
        self.assertIn("Save the current settings as “night”", t)
        results = build_results(parse_query("night"), SCREENS, "sc", DEFAULT_PROFILES)
        self.assertIn("1: ☀ 10% HDR off", results[0].sub_tooltip or "")  # what it sets
        self.assertIn("1: ☀ 40→10%", results[0].sub or "")  # what it changes now
        self.assertIn("2: ☀ 40→10% HDR on→off", results[0].sub or "")

    def test_save_and_delete_views(self) -> None:
        self.assertEqual(titles("profile save gaming", profiles=DEFAULT_PROFILES), ["Save current settings as “gaming”"])
        self.assertEqual(titles("night save", profiles=DEFAULT_PROFILES), ["Save current settings as “night” (replaces it)"])
        self.assertIn("already has its default settings", titles("profile reset night", profiles=DEFAULT_PROFILES)[0])
        custom = {**DEFAULT_PROFILES, "gaming": {"*": {"brightness": 90}}}
        self.assertEqual(titles("profile delete gaming", profiles=custom, customized=frozenset({"gaming"})), ["Delete profile “gaming”"])
        self.assertIn("No profile “nope”", titles("profile nope", profiles=DEFAULT_PROFILES)[0])

    def test_profile_rows_have_no_quick_action_menu(self) -> None:
        for r in build_results(parse_query("profile"), SCREENS, "sc", DEFAULT_PROFILES):
            self.assertFalse(getattr(r, "menu_screens", None))


class StoreTests(unittest.TestCase):
    def test_a_damaged_file_falls_back_on_the_backup(self) -> None:
        for damage in ("{not json", "", "[]"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp))
                store.save_profile("Gaming", {"*": {"brightness": 90}})
                store.save_profile("Work", {"*": {"brightness": 60}})
                (Path(tmp) / "screen_control.json").write_text(damage, encoding="utf-8")

                again = Store(Path(tmp))
                self.assertIn("Gaming", again.profiles())
                self.assertEqual(again.backup.exists(), True)
                self.assertIn("Gaming", json.loads(again.path.read_text(encoding="utf-8"))["profiles"])

    def test_a_crash_before_the_file_is_back_keeps_the_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.set_nickname("A", "Desk")
            store.set_nickname("B", "Side")
            store.path.unlink()
            self.assertEqual(Store(Path(tmp)).nickname("A"), "Desk")

    def test_another_process_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Store(Path(tmp)), Store(Path(tmp))
            new.save_profile("Gaming", {"*": {"brightness": 90}})
            old.set_nickname("A", "Desk")  # still running, started before that save
            self.assertIn("Gaming", old.profiles())
            again = Store(Path(tmp))
            self.assertIn("Gaming", again.profiles())
            self.assertEqual(again.nickname("A"), "Desk")
            self.assertEqual(new.nickname("A"), "Desk")  # picked up without a restart

    def test_a_locked_file_is_not_treated_as_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_profile("Gaming", {"*": {"brightness": 90}})
            before = store.path.read_text(encoding="utf-8")
            with mock.patch("screen_control.store.time.sleep"), mock.patch.object(
                Path, "read_text", side_effect=PermissionError("locked")
            ):
                locked = Store(Path(tmp))  # starts anyway
                self.assertEqual(locked.profiles(), DEFAULT_PROFILES)
                with self.assertRaises(OSError):  # refuses to save over data it can't see
                    locked.set_nickname("A", "Desk")
            self.assertEqual(store.path.read_text(encoding="utf-8"), before)
            self.assertFalse(store.path.with_suffix(".bad.json").exists())
            self.assertIn("Gaming", locked.profiles())  # back once the lock is gone

    def test_a_short_lock_is_waited_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.set_nickname("A", "Desk")
            real, calls = os.replace, []

            def locked_once(*args):  # noqa: ANN002, ANN202
                calls.append(args)
                if len(calls) == 1:
                    raise PermissionError("locked")
                return real(*args)

            with mock.patch("screen_control.store.os.replace", locked_once), mock.patch("screen_control.store.time.sleep"):
                store.set_nickname("B", "Side")
            self.assertEqual(Store(Path(tmp)).nickname("B"), "Side")

    def test_temp_files_left_by_a_crash_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old, recent = Path(tmp) / "screen_control.1.tmp", Path(tmp) / "screen_control.2.tmp"
            old.write_text("x", encoding="utf-8")
            recent.write_text("x", encoding="utf-8")
            os.utime(old, (1, 1))
            Store(Path(tmp))
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())

    def test_bad_profile_values_are_ignored_but_kept(self) -> None:
        bad = {
            "*": {"brightness": "x", "contrast": 150, "hdr": "yes"},
            "a": 5,
            "b": None,
            "c": {"brightness": True, "contrast": float("nan")},
            "d": {"brightness": 49.6, "hdr": False, "other": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen_control.json"
            path.write_text(json.dumps({"profiles": {"Broken": bad, "Text": "oops", "Good": {"*": {"brightness": 5}}}}), encoding="utf-8")
            store = Store(Path(tmp))
            profiles = store.profiles()
            self.assertEqual(
                profiles["Broken"], {"*": {"contrast": 100}, "c": {}, "d": {"brightness": 50, "hdr": False}}
            )
            self.assertEqual(profiles["Text"], {})
            self.assertEqual(profiles["Good"], {"*": {"brightness": 5}})
            for text in ("", "p", "Broken", "Text"):  # the list keeps working
                build_results(parse_query(text, profiles), SCREENS, "sc", profiles, frozenset(profiles), None)
            profile_plan(profiles["Broken"], SCREENS)

            store.set_nickname("A", "Desk")  # saving leaves them for the user to fix
            saved = json.loads(path.read_text(encoding="utf-8"))["profiles"]
            self.assertEqual(saved["Text"], "oops")
            self.assertEqual(saved["Broken"]["a"], 5)

    def test_a_newer_versions_data_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "screen_control.json").write_text('{"version": 2, "future": [1]}', encoding="utf-8")
            store = Store(Path(tmp))
            store.set_nickname("A", "Desk")
            saved = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual((saved["version"], saved["future"]), (2, [1]))

    def test_round_trip_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.set_nickname("GSM5D24#123456", "  Gaming   Desk ")
            store.save_profile("Gaming", {"*": {"brightness": 90}})
            store.save_profile("night", {"*": {"brightness": 5}})

            again = Store(Path(tmp))
            self.assertEqual(again.nickname("GSM5D24#123456"), "Gaming Desk")
            self.assertEqual(again.profiles()["Gaming"], {"*": {"brightness": 90}})
            self.assertEqual(again.profiles()["night"], {"*": {"brightness": 5}})
            self.assertTrue(again.is_customized("night"))

            self.assertTrue(again.delete_profile("night"))  # reset built-in
            self.assertEqual(again.profiles()["night"], DEFAULT_PROFILES["night"])
            again.set_nickname("GSM5D24#123456", "")
            self.assertIsNone(again.nickname("GSM5D24#123456"))

            (Path(tmp) / "screen_control.json").write_text("{not json", encoding="utf-8")
            (Path(tmp) / "screen_control.bak.json").unlink()
            broken = Store(Path(tmp))  # must not raise
            self.assertEqual(broken.profiles(), DEFAULT_PROFILES)
            self.assertTrue((Path(tmp) / "screen_control.bad.json").exists())

    def test_data_dir(self) -> None:
        flow = Path("C:/Users/x/AppData/Roaming/FlowLauncher/Plugins/ScreenControl")
        self.assertEqual(data_dir(flow), Path("C:/Users/x/AppData/Roaming/FlowLauncher/Settings/Plugins/ScreenControl"))
        self.assertEqual(data_dir(Path("C:/dev/repo")), Path("C:/dev/repo/data"))

    def test_data_saved_under_the_old_name_is_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flow = Path(tmp) / "FlowLauncher"
            plugin_root = flow / "Plugins" / "ScreenControl"
            old = flow / "Settings" / "Plugins" / "Screen Control"
            old.mkdir(parents=True)
            (old / "screen_control.json").write_text('{"nicknames": {"X#1": "Desk"}}', encoding="utf-8")
            new_dir = data_dir(plugin_root)
            self.assertEqual(new_dir.name, "ScreenControl")
            self.assertEqual(Store(new_dir).nickname("X#1"), "Desk")
            self.assertFalse((old / "screen_control.json").exists())

    def test_identity_survives_port_change(self) -> None:
        a = replace(_screen(1, "LG", ""), identity="GSM5D24#123456")
        b = replace(a, key="other-port-path", device_path="other-port-path")
        self.assertEqual(a.stable_id, b.stable_id)


class DdcMessageTests(unittest.TestCase):
    def test_messages(self) -> None:
        self.assertIn("corrupted", describe_error(0xC026258A))
        self.assertIn("power button", describe_error(0xC026258A))
        self.assertIn("contrast", describe_error(0xC0262584, VCP_CONTRAST))
        self.assertIn("Remote Desktop", describe_error(0xC02625E0))
        self.assertIn("enabled in its on-screen menu", describe_error(0xC0262581))
        self.assertIn("enabled in its on-screen menu", describe_error(5))
        self.assertIn("corrupted", describe_error(-1071241846))  # same code as a signed int


if __name__ == "__main__":
    unittest.main()
