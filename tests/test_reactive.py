"""Profile completion, live previews of profile changes, and the feedback / undo banner."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from screen_control.feedback import Feedback, merge_restore
from screen_control.query_parser import Action, parse_query, profile_name_problem
from screen_control.results import (
    DeleteProfileResult,
    LevelResult,
    NameResult,
    ProfileResult,
    SaveProfileResult,
    UndoResult,
)
from screen_control.screens import Feature, Level
from screen_control.store import DEFAULT_PROFILES, Store
from screen_control.views import build_results

from .test_logic import SCREENS

CUSTOM = {**DEFAULT_PROFILES, "gaming": {"*": {"brightness": 90}}, "gaming night": {"*": {"brightness": 20}}}


def rows(text: str, profiles=CUSTOM, screens=SCREENS, customized=frozenset({"gaming", "gaming night"}), feedback=None):  # noqa: ANN001
    return build_results(parse_query(text, profiles), screens, "sc", profiles, customized, feedback)


def titles(text: str, **kwargs) -> list[str]:  # noqa: ANN003
    return [r.title or "" for r in rows(text, **kwargs)]


class CustomProfileParseTests(unittest.TestCase):
    def test_own_profiles_by_exact_name(self) -> None:
        q = parse_query("gaming", CUSTOM)
        self.assertEqual((q.action, q.profile_name, q.profile_op), (Action.PROFILE, "gaming", "apply"))
        q = parse_query("gaming night save", CUSTOM)
        self.assertEqual((q.profile_name, q.profile_op), ("gaming night", "save"))
        self.assertEqual(parse_query("gaming delete", CUSTOM).profile_op, "delete")

    def test_without_the_profile_it_is_a_screen_filter(self) -> None:
        self.assertEqual(parse_query("gaming").filters, ["gaming"])
        self.assertEqual(parse_query("gaming 50", CUSTOM).action, Action.LEVEL)  # not the exact name

    def test_names_that_already_mean_something_are_refused(self) -> None:
        for name in ("hdr", "c", "50", "#2", "name x", "profile", "id", "reset", "all", "on"):
            self.assertIsNotNone(profile_name_problem(name), name)
        for name in ("gaming", "night", "work late", "o"):
            self.assertIsNone(profile_name_problem(name), name)


class ShortCommandTests(unittest.TestCase):
    def test_letters_that_fit_one_command(self) -> None:
        for text, action in (("p", Action.PROFILE), ("pro", Action.PROFILE), ("r", Action.RESET),
                             ("s", Action.SETTINGS), ("he", Action.HELP), ("na", Action.NAME)):
            self.assertIs(parse_query(text).action, action, text)
        self.assertEqual(parse_query("d").profile_name, "day")
        self.assertEqual(parse_query("m").profile_name, "movie")
        self.assertEqual(parse_query("ni").profile_name, "night")

    def test_short_words_with_the_rest_of_the_command(self) -> None:
        q = parse_query("p save Gaming")
        self.assertEqual((q.action, q.profile_op, q.profile_name), (Action.PROFILE, "save", "Gaming"))
        self.assertEqual(parse_query("ni save").profile_op, "save")
        q = parse_query("p sa gaming")
        self.assertEqual((q.profile_op, q.profile_name), ("save", "gaming"))
        self.assertEqual(parse_query("p de gaming").profile_op, "delete")
        q = parse_query("p sa", ["sandbox"])  # a profile starts with those letters
        self.assertEqual((q.profile_op, q.profile_name), ("apply", "sa"))
        q = parse_query("na #2 My Desk")
        self.assertEqual((q.action, q.screen_number, q.nickname), (Action.NAME, 2, "My Desk"))

    def test_existing_meanings_stay(self) -> None:
        self.assertIs(parse_query("h").action, Action.HDR)
        self.assertIs(parse_query("b").feature, Feature.BRIGHTNESS)
        self.assertIs(parse_query("c").feature, Feature.CONTRAST)
        self.assertIs(parse_query("i").action, Action.IDENTIFY)
        self.assertEqual(parse_query("p 50").filters, ["p"])  # still a screen word with a value
        self.assertEqual(parse_query("r 50").filters, ["r"])
        self.assertEqual(parse_query("pri").filters, ["pri"])

    def test_conflicts_do_not_resolve(self) -> None:
        self.assertIs(parse_query("n").action, Action.OVERVIEW)  # name, night
        self.assertIs(parse_query("p", ["play"]).action, Action.OVERVIEW)  # profile, play
        self.assertIs(parse_query("pr", ["play"]).action, Action.PROFILE)
        self.assertIn("sc profile", titles("p", profiles={**CUSTOM, "play": {"*": {"brightness": 1}}}))

    def test_screen_the_letters_could_mean_is_one_row_away(self) -> None:
        self.assertEqual(titles("r"), ["Reset ScreenControl", "2 · LG ULTRAGEAR"])  # right
        self.assertEqual(titles("p")[0], "Apply profile “day”")
        self.assertIn("1 · FQ320", titles("p"))  # primary

    def test_short_words_cannot_become_profile_names(self) -> None:
        self.assertIsNotNone(profile_name_problem("p"))
        self.assertIsNotNone(profile_name_problem("se"))


class OverviewTests(unittest.TestCase):
    def test_settings_panel_comes_right_after_the_screens(self) -> None:
        self.assertEqual(titles("")[:3], ["1 · FQ320", "2 · LG ULTRAGEAR", "Open the settings panel"])
        self.assertNotIn("Open the settings panel", titles("lg"))


class CompletionTests(unittest.TestCase):
    def test_half_typed_profile_names(self) -> None:
        self.assertEqual(titles("gam")[:2], ["sc gaming", "sc gaming night"])
        self.assertIn("Profile · Changes", rows("gam")[0].sub or "")
        self.assertEqual(titles("nig")[0], "Apply profile “night”")

    def test_unknown_name_in_profile_command_suggests(self) -> None:
        t = titles("profile gam")
        self.assertTrue(t[0].startswith("Apply profile “gaming”"), t)
        self.assertIn("Save the current settings as “gam”", t)
        self.assertIn("No profile “nope”", titles("profile nope")[0])

    def test_save_offers_existing_profiles_to_replace(self) -> None:
        t = titles("profile save")
        self.assertEqual(t[0], "Type a profile name to save")
        self.assertIn("Replace “night” with the current settings", t)
        t = titles("profile save gam")
        self.assertEqual(t[0], "Save current settings as “gam”")
        self.assertEqual(t[1:], ["Replace “gaming” with the current settings", "Replace “gaming night” with the current settings"])

    def test_save_refuses_command_names(self) -> None:
        t = titles("profile save hdr")
        self.assertIn("already a ScreenControl command", t[0])
        self.assertFalse(any(isinstance(r, SaveProfileResult) for r in rows("profile save hdr")))

    def test_save_warns_when_a_profile_takes_a_screen_word(self) -> None:
        self.assertIn("instead of picking 1 · FQ320", rows("profile save left")[0].sub or "")

    def test_delete_lists_what_can_be_deleted(self) -> None:
        t = titles("profile delete")
        self.assertEqual(t, ["Delete “gaming”", "Delete “gaming night”"])
        self.assertEqual(titles("profile delete", profiles=DEFAULT_PROFILES, customized=frozenset()),
                         ["No profile of your own to delete yet"])
        self.assertIn("undo", rows("profile delete gaming")[0].sub or "")


class LivePreviewTests(unittest.TestCase):
    def test_active_profile_is_marked(self) -> None:
        bright = [replace(s, brightness=Level(90)) for s in SCREENS]
        t = titles("profile", screens=bright)
        self.assertIn("Apply profile “gaming”   (✓ active)", t)
        sub = next(r.sub for r in rows("profile", screens=bright) if "“gaming”" in (r.title or ""))
        self.assertEqual(sub, "Already active: your screens match it")
        overview = rows("", screens=bright)
        self.assertIn("Active: gaming", next(r.sub for r in overview if r.title == "Profiles") or "")

    def test_saving_over_a_profile_shows_what_changes(self) -> None:
        self.assertIn("Updates 1: ☀ 10→40%", rows("night save")[0].sub or "")
        dim = [replace(s, brightness=Level(20)) for s in SCREENS]
        same = {**CUSTOM, "gaming night": {s.stable_id: {"brightness": 20, "contrast": 70} for s in SCREENS}}
        for s, r in zip(dim, SCREENS):
            same["gaming night"][s.stable_id]["hdr"] = bool(r.hdr and r.hdr.enabled)
        self.assertEqual(rows("gaming night save", profiles=same, screens=dim)[0].sub, "Same as the saved settings: nothing would change")
        self.assertTrue((rows("profile save fresh")[0].sub or "").startswith("Saves 1: ☀ 40% ◑ 70% HDR off"))


    def test_screens_without_ddc_are_not_promised_changes(self) -> None:
        no_ddc = [SCREENS[0], replace(SCREENS[1], brightness=Level(error="no DDC"))]
        sub = rows("night", screens=no_ddc)[0].sub or ""
        self.assertIn("1: ☀ 40→10%", sub)
        self.assertIn("2: HDR on→off", sub)
        self.assertNotIn("2: ☀", sub)


class BannerTests(unittest.TestCase):
    def test_banner_comes_first_and_undoes(self) -> None:
        fb = Feedback("Brightness 50% — all 2 screens", "Was 1: ☀ 40%", "sc 50", restore={"path1": {"brightness": 40}})
        results = rows("50", feedback=fb)
        self.assertIsInstance(results[0], UndoResult)
        self.assertEqual(results[0].title, "✓ Brightness 50% — all 2 screens")
        self.assertIn("Enter: undo", results[0].sub or "")
        self.assertGreater(results[0].score, results[1].score)

    def test_banner_without_undo_is_information(self) -> None:
        results = rows("", feedback=Feedback("Undone: back to the previous values", "", "sc"))
        self.assertNotIsInstance(results[0], UndoResult)
        self.assertTrue((results[0].title or "").startswith("✓ "))

    def test_banner_belongs_to_its_list(self) -> None:
        fb = Feedback("x", "", "sc 50")
        self.assertTrue(fb.shown_for("sc 50 "))
        self.assertFalse(fb.shown_for("sc 40"))
        fb.at -= 3600
        self.assertFalse(fb.shown_for("sc 50"))

    def test_repeated_changes_undo_to_the_start(self) -> None:
        first = Feedback("", "", "sc +10", restore={"path1": {"brightness": 40}})
        merged = merge_restore(first, "sc +10", {"path1": {"brightness": 50}, "path2": {"brightness": 60}})
        self.assertEqual(merged, {"path1": {"brightness": 40}, "path2": {"brightness": 60}})
        self.assertEqual(merge_restore(first, "sc -10", {"path1": {"brightness": 50}}), {"path1": {"brightness": 50}})


class _FakeService:
    def __init__(self) -> None:
        self.list = list(SCREENS)
        self.applied: list[dict] = []

    async def screens(self, max_age: float = 4.0):  # noqa: ANN201
        return list(self.list)

    def _set(self, key: str, **changes) -> None:  # noqa: ANN003
        self.list = [replace(s, **changes) if s.key == key else s for s in self.list]

    async def set_levels(self, keys, feature, *, value=None, delta=None):  # noqa: ANN001, ANN201
        out = {}
        for s in self.list:
            if s.key in keys:
                new = value if value is not None else max(0, min(100, (s.level(feature).value or 0) + delta))
                self._set(s.key, **{feature.value: Level(new)})
                out[s.key] = new
        return out

    async def apply_settings(self, plan):  # noqa: ANN001, ANN201
        self.applied.append(plan)
        for key, settings in plan.items():
            for feature in Feature:
                if feature.value in settings:
                    self._set(key, **{feature.value: Level(settings[feature.value])})
        return {}


def _plugin(tmp: str, text: str):  # noqa: ANN202
    queries: list[str] = []
    notes: list[str] = []

    async def change_query(q: str, requery: bool = False) -> None:
        queries.append(q)

    async def show_notification(title: str, text: str) -> None:
        notes.append(text)

    plugin = SimpleNamespace(
        service=_FakeService(),
        store=Store(Path(tmp)),
        feedback=None,
        api=SimpleNamespace(change_query=change_query, show_notification=show_notification),
        last_query=SimpleNamespace(raw_text=text, keyword="sc", text=text.removeprefix("sc ")),
    )
    return plugin, queries, notes


def _run(result, plugin):  # noqa: ANN001, ANN202
    result.plugin = plugin
    return asyncio.run(result.callback())


class ActionFeedbackTests(unittest.TestCase):
    def test_level_change_reports_and_undoes_back_to_the_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, queries, notes = _plugin(tmp, "sc +10")
            for _ in range(3):
                _run(LevelResult(SCREENS, Feature.BRIGHTNESS, delta=10), plugin)
            self.assertEqual(plugin.feedback.title, "Brightness 70% — all 2 screens")
            self.assertEqual(plugin.feedback.sub, "Was 1: ☀ 40% · 2: ☀ 40%")
            self.assertEqual(notes, [])  # success is shown in the list, not as a toast
            _run(UndoResult(), plugin)
            self.assertEqual(plugin.service.applied[-1], {"path1": {"brightness": 40}, "path2": {"brightness": 40}})
            self.assertEqual([s.brightness.value for s in plugin.service.list], [40, 40])
            self.assertFalse(plugin.feedback.can_undo)
            self.assertEqual(queries[-1], "sc +10")

    def test_profile_apply_restores_only_what_it_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _, _ = _plugin(tmp, "sc gaming")
            _run(ProfileResult("gaming", CUSTOM["gaming"], SCREENS), plugin)
            self.assertEqual(plugin.feedback.title, "Applied profile “gaming”")
            self.assertEqual(plugin.feedback.restore, {"path1": {"brightness": 40}, "path2": {"brightness": 40}})

    def test_applying_an_active_profile_offers_no_undo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _, _ = _plugin(tmp, "sc gaming")
            plugin.service.list = [replace(s, brightness=Level(90)) for s in SCREENS]
            _run(ProfileResult("gaming", CUSTOM["gaming"], SCREENS), plugin)
            self.assertEqual(plugin.feedback.title, "“gaming” was already active")
            self.assertFalse(plugin.feedback.can_undo)

    def test_save_then_undo_removes_a_new_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, queries, _ = _plugin(tmp, "sc profile save work")
            _run(SaveProfileResult("work", SCREENS), plugin)
            self.assertIn("work", plugin.store.profiles())
            self.assertEqual(plugin.feedback.title, "Saved profile “work” — 2 screens")
            self.assertEqual(queries[-1], "sc work")
            self.assertTrue(plugin.feedback.shown_for("sc work"))
            _run(UndoResult(), plugin)
            self.assertNotIn("work", plugin.store.profiles())
            self.assertEqual(queries[-1], "sc profile")

    def test_overriding_a_default_then_undo_restores_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _, _ = _plugin(tmp, "sc night save")
            _run(SaveProfileResult("night", SCREENS), plugin)
            self.assertEqual(plugin.feedback.title, "Updated profile “night” — 2 screens")
            _run(UndoResult(), plugin)
            self.assertEqual(plugin.store.profiles()["night"], DEFAULT_PROFILES["night"])
            self.assertFalse(plugin.store.is_customized("night"))

    def test_delete_then_undo_brings_it_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _, _ = _plugin(tmp, "sc profile delete work")
            plugin.store.save_profile("work", {"*": {"brightness": 55}})
            _run(DeleteProfileResult("work"), plugin)
            self.assertEqual(plugin.feedback.title, "Deleted profile “work”")
            _run(UndoResult(), plugin)
            self.assertEqual(plugin.store.profiles()["work"], {"*": {"brightness": 55}})

    def test_nickname_undo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _, _ = _plugin(tmp, "sc name #2 Gaming")
            _run(NameResult(SCREENS[1], "Gaming"), plugin)
            self.assertEqual(plugin.feedback.title, "Screen 2 is now “Gaming”")
            self.assertEqual(plugin.feedback.query, "sc ")
            _run(UndoResult(), plugin)
            self.assertIsNone(plugin.store.nickname(SCREENS[1].stable_id))


class KeywordRegistrationTests(unittest.TestCase):
    def _register(self, plugins, own=("sc",)):  # noqa: ANN001, ANN202
        from screen_control.plugin import ScreenControlPlugin

        added: list[str] = []

        async def get_all_plugins():  # noqa: ANN202
            if isinstance(plugins, Exception):
                raise plugins
            return plugins

        async def add_keyword(_id: str, keyword: str) -> None:
            added.append(keyword)

        fake = SimpleNamespace(
            metadata=SimpleNamespace(id="me", keywords=list(own)),
            api=SimpleNamespace(get_all_plugins=get_all_plugins, add_keyword=add_keyword),
        )
        fake._named_keywords = lambda: ScreenControlPlugin._named_keywords(fake)
        asyncio.run(ScreenControlPlugin._receive_any_case(fake))
        return added

    def test_only_the_hidden_keyword_is_added(self) -> None:
        self.assertEqual(self._register([SimpleNamespace(id="me", keywords=["sc"])]), ["*"])

    def test_a_restart_does_not_register_twice(self) -> None:
        self.assertEqual(self._register([SimpleNamespace(id="me", keywords=["sc", "*"])]), [])

    def test_still_registers_if_flow_cannot_say(self) -> None:
        self.assertEqual(self._register(RuntimeError("no")), ["*"])

    def test_a_global_only_plugin_changes_nothing(self) -> None:
        self.assertEqual(self._register([], own=("*",)), [])


class CaseTests(unittest.TestCase):
    MIXED = {**DEFAULT_PROFILES, "Gaming": {"*": {"brightness": 90}}, "gaming": {"*": {"brightness": 40}}}

    def test_commands_ignore_case(self) -> None:
        self.assertIs(parse_query("HDR OFF").action, Action.HDR)
        self.assertEqual(parse_query("C 40").feature, parse_query("c 40").feature)
        self.assertIs(parse_query("RESET").action, Action.RESET)
        self.assertIs(parse_query("SeTTings").action, Action.SETTINGS)
        self.assertIs(parse_query("ID").action, Action.IDENTIFY)
        self.assertIs(parse_query("P").action, Action.PROFILE)
        self.assertEqual(parse_query("LG 30").filters, ["lg"])
        q = parse_query("PROFILE SAVE Gaming")
        self.assertEqual((q.action, q.profile_op, q.profile_name), (Action.PROFILE, "save", "Gaming"))

    def test_profile_names_are_case_sensitive(self) -> None:
        self.assertEqual(parse_query("Gaming", self.MIXED).profile_name, "Gaming")
        self.assertEqual(parse_query("gaming", self.MIXED).profile_name, "gaming")
        self.assertIsNone(parse_query("GAMING", self.MIXED).profile_name)
        self.assertEqual(parse_query("night").profile_name, "night")
        self.assertIsNone(parse_query("NIGHT").profile_name)  # built-in names too
        self.assertIsNone(parse_query("Nig").profile_name)
        q = parse_query("gaming SAVE", self.MIXED)  # the command word still ignores case
        self.assertEqual((q.profile_name, q.profile_op), ("gaming", "save"))

    def test_completion_respects_case(self) -> None:
        self.assertEqual([r.title for r in rows("Gam", self.MIXED)][:1], ["sc Gaming"])
        self.assertEqual([r.title for r in rows("gam", self.MIXED)][:1], ["sc gaming"])
        self.assertNotIn("sc night", [r.title for r in rows("NIG", self.MIXED)])

    def test_a_capitalised_name_is_its_own_profile(self) -> None:
        from screen_control.query_parser import profile_name_problem

        self.assertIsNone(profile_name_problem("Gaming"))
        self.assertIsNone(profile_name_problem("Night"))
        self.assertIsNotNone(profile_name_problem("HDR"))  # a command, whatever its case
        self.assertIsNotNone(profile_name_problem("Reset"))


if __name__ == "__main__":
    unittest.main()
