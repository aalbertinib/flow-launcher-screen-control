"""Hardware-free tests.  python -m unittest discover -s tests -t ."""

from __future__ import annotations

import unittest

from screen_control.query_parser import Action, HdrMode, parse_query
from screen_control.screens import Feature, Level, Screen, describe_positions
from screen_control.views import build_results
from screen_control.win32.displayconfig import HdrState
from screen_control.win32.edid import device_instance_id, parse_edid


class ParserTests(unittest.TestCase):
    def test_empty_is_overview(self) -> None:
        q = parse_query("")
        self.assertIs(q.action, Action.OVERVIEW)
        self.assertFalse(q.has_selector)

    def test_bare_number_is_brightness(self) -> None:
        q = parse_query("50")
        self.assertIs(q.action, Action.LEVEL)
        self.assertIs(q.feature, Feature.BRIGHTNESS)
        self.assertEqual(q.value, 50)

    def test_zero_is_allowed(self) -> None:  # the old plugin only accepted 1-100
        self.assertEqual(parse_query("0").value, 0)

    def test_percent_and_relative(self) -> None:
        self.assertEqual(parse_query("70%").value, 70)
        self.assertEqual(parse_query("+10").delta, 10)
        self.assertEqual(parse_query("-5").delta, -5)

    def test_out_of_range(self) -> None:
        self.assertIsNotNone(parse_query("150").error)

    def test_two_values(self) -> None:
        self.assertIsNotNone(parse_query("50 60").error)

    def test_contrast_prefixes_and_aliases(self) -> None:
        for text in ("c 40", "con 40", "contrast 40", "contraste 40", "40 c"):
            q = parse_query(text)
            self.assertIs(q.feature, Feature.CONTRAST, text)
            self.assertEqual(q.value, 40, text)

    def test_contrast_without_value_lists_levels(self) -> None:
        q = parse_query("c")
        self.assertIs(q.action, Action.LEVEL)
        self.assertFalse(q.has_level_change)

    def test_hdr(self) -> None:
        self.assertIs(parse_query("hdr").action, Action.HDR)
        self.assertIsNone(parse_query("hdr").hdr_mode)
        self.assertIs(parse_query("hdr on").hdr_mode, HdrMode.ON)
        self.assertIs(parse_query("hdr off").hdr_mode, HdrMode.OFF)
        self.assertIs(parse_query("h of").hdr_mode, HdrMode.OFF)
        self.assertIs(parse_query("off").action, Action.HDR)

    def test_hdr_partial_word_does_not_filter(self) -> None:
        q = parse_query("hdr o")
        self.assertIs(q.action, Action.HDR)
        self.assertEqual(q.filters, [])

    def test_hdr_with_number_is_error(self) -> None:
        self.assertIsNotNone(parse_query("hdr 50").error)

    def test_identify(self) -> None:
        for text in ("id", "i", "identify", "which"):
            self.assertIs(parse_query(text).action, Action.IDENTIFY, text)

    def test_selectors(self) -> None:
        q = parse_query("#2 c 30")
        self.assertEqual(q.screen_number, 2)
        self.assertEqual(q.value, 30)
        q = parse_query("lg left 40")
        self.assertEqual(q.filters, ["lg", "left"])
        self.assertEqual(parse_query("hdmi").filters, ["hdmi"])  # not "hdr"
        self.assertEqual(parse_query("internal").filters, ["internal"])  # not "identify"
        self.assertEqual(parse_query("all 30").filters, [])

    def test_mixed_settings_is_error(self) -> None:
        self.assertIsNotNone(parse_query("c b 30").error)


class PositionTests(unittest.TestCase):
    def test_single(self) -> None:
        self.assertEqual(describe_positions([(0, 0, 1920, 1080)]), [""])

    def test_side_by_side(self) -> None:
        rects = [(0, 0, 2560, 1440), (2560, 0, 4480, 1080)]
        self.assertEqual(describe_positions(rects), ["left", "right"])

    def test_three_in_a_row(self) -> None:
        rects = [(-1920, 0, 0, 1080), (0, 0, 2560, 1440), (2560, 0, 4480, 1080)]
        self.assertEqual(describe_positions(rects), ["left", "middle", "right"])

    def test_stacked(self) -> None:
        rects = [(0, -1080, 1920, 0), (0, 0, 1920, 1080)]
        self.assertEqual(describe_positions(rects), ["top", "bottom"])

    def test_laptop_below_offset(self) -> None:
        rects = [(0, 0, 2560, 1440), (2560, 1440, 4480, 2520)]
        self.assertEqual(describe_positions(rects), ["top-left", "bottom-right"])


def _edid(name: bytes = b"LG ULTRAGEAR", serial: bytes = b"123456") -> bytes:
    data = bytearray(128)
    data[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    data[8:10] = (0x1E6D).to_bytes(2, "big")  # "GSM"
    data[10:12] = (0x5D24).to_bytes(2, "little")
    data[17] = 36  # 2026
    data[21], data[22] = 60, 34
    data[72:77] = b"\x00\x00\x00\xfc\x00"
    data[77:90] = (name + b"\x0a").ljust(13, b" ")
    data[90:95] = b"\x00\x00\x00\xff\x00"
    data[95:108] = (serial + b"\x0a").ljust(13, b" ")
    return bytes(data)


class EdidTests(unittest.TestCase):
    def test_parse(self) -> None:
        info = parse_edid(_edid())
        assert info is not None
        self.assertEqual(info.manufacturer_id, "GSM")
        self.assertEqual(info.product_code, 0x5D24)
        self.assertEqual(info.model_name, "LG ULTRAGEAR")
        self.assertEqual(info.serial_number, "123456")
        self.assertEqual(info.year, 2026)
        self.assertAlmostEqual(info.diagonal_inches or 0, 27.2, places=1)

    def test_garbage(self) -> None:
        self.assertIsNone(parse_edid(b""))
        self.assertIsNone(parse_edid(b"\x00" * 128))

    def test_instance_id(self) -> None:
        path = r"\\?\DISPLAY#GSM5D24#4&1a2b3c4&0&UID1234#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
        self.assertEqual(device_instance_id(path), r"DISPLAY\GSM5D24\4&1a2b3c4&0&UID1234")
        self.assertIsNone(device_instance_id("garbage"))


def _screen(
    number: int, name: str, position: str, *, vendor: str = "LG", hdr: bool | None = False, ddc: bool = True
) -> Screen:
    return Screen(
        key=f"path{number}",
        number=number,
        name=name,
        vendor=vendor,
        gdi_device_name=f"\\\\.\\DISPLAY{number}",
        hmonitor=number,
        adapter_id=(1, 0),
        target_id=number,
        device_path=f"path{number}",
        connection="HDMI",
        internal=False,
        width=1920,
        height=1080,
        refresh_hz=144.0,
        scale_percent=100,
        rect=(0, 0, 1920, 1080),
        primary=number == 1,
        position=position,
        diagonal_inches=27.0,
        serial=None,
        hdr=None if hdr is None else HdrState(True, hdr, hdr, False, 10, True),
        brightness=Level(40) if ddc else Level(error="no DDC"),
        contrast=Level(70) if ddc else Level(error="no DDC"),
    )


SCREENS = [_screen(1, "FQ320", "left", vendor="WAM"), _screen(2, "LG ULTRAGEAR", "right", hdr=True)]


def titles(text: str, screens: list[Screen] = SCREENS) -> list[str]:
    return [(r.title_tooltip if r.progress_bar else r.title) or "" for r in build_results(parse_query(text), screens, "sc")]


class ViewTests(unittest.TestCase):
    def test_overview(self) -> None:
        t = titles("")
        self.assertEqual(t[:2], ["1 · FQ320", "2 · LG ULTRAGEAR"])
        self.assertIn("Identify — all 2 screens", t)

    def test_scores_keep_order(self) -> None:
        results = build_results(parse_query(""), SCREENS, "sc")
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_set_all_then_each(self) -> None:
        t = titles("50")
        self.assertTrue(t[0].startswith("Set brightness to 50% — all 2 screens"))
        self.assertEqual(len(t), 3)

    def test_filter_to_one(self) -> None:
        t = titles("lg 30")
        self.assertEqual(t, ["Set brightness to 30% — 2 · LG ULTRAGEAR"])

    def test_detail_view(self) -> None:
        t = titles("#2")
        self.assertEqual(t[0], "Brightness 40%")
        self.assertEqual(t[1], "Contrast 70%")
        self.assertIn("HDR is on", t[2])

    def test_unknown_screen(self) -> None:
        self.assertIn("There is no screen #5", titles("#5 50")[0])
        self.assertIn("No screen matches", titles("dell")[0])

    def test_hdr_on_skips_already_on(self) -> None:
        t = titles("hdr on")
        self.assertEqual(
            t, ["Windows HDR: turn on everywhere", "Turn HDR on — 1 · FQ320", "HDR already on — 2 · LG ULTRAGEAR"]
        )

    def test_hdr_toggle_all_then_each(self) -> None:
        # Mixed (1 off, 2 on): Windows' rule turns HDR off everywhere; "on everywhere" is offered too.
        t = titles("hdr")
        self.assertEqual(
            t,
            [
                "Windows HDR: turn off everywhere",
                "Windows HDR: turn on everywhere",
                "Turn HDR on — 1 · FQ320",
                "Turn HDR off — 2 · LG ULTRAGEAR",
            ],
        )
        both_off = [_screen(1, "A", "left"), _screen(2, "B", "right")]
        self.assertEqual(titles("hdr", both_off)[:2], ["Windows HDR: turn on everywhere", "Turn HDR on — 1 · A"])
        sub = build_results(parse_query("hdr"), SCREENS, "sc")[0].sub or ""
        self.assertIn("Windows HDR is on for screen 2", sub)

    def test_windows_hdr_with_one_hdr_screen_and_phrasing(self) -> None:
        one = [_screen(1, "TV", "", hdr=None), _screen(2, "LG", "", hdr=False)]
        self.assertEqual(titles("hdr", one)[0], "Windows HDR: turn on")
        self.assertEqual(titles("windows hdr off")[0], "Windows HDR: turn off everywhere")
        self.assertEqual(titles("win hdr")[0], "Windows HDR: turn off everywhere")
        # A specific screen chosen: no system-wide row.
        self.assertEqual(titles("#1 hdr"), ["Turn HDR on — 1 · FQ320"])

    def test_hdr_unsupported_screen_is_listed_not_actionable(self) -> None:
        screens = [_screen(1, "TV", "", hdr=None), _screen(2, "LG", "", hdr=True)]
        self.assertEqual(
            titles("hdr", screens), ["Windows HDR: turn off", "Turn HDR off — 2 · LG", "HDR state unknown — 1 · TV"]
        )

    def test_level_list_offers_all_then_each(self) -> None:
        self.assertEqual(titles("c"), ["Contrast — all 2 screens", "Contrast 70%", "Contrast 70%"])
        self.assertEqual(titles("#1 c"), ["Contrast 70%"])  # one screen selected: no "all" row

    def test_every_multi_screen_action_offers_all_first(self) -> None:
        for text in ("50", "+10", "c 40", "b", "c", "id"):
            self.assertIn("all", titles(text)[0].lower(), text)
        for text in ("hdr", "hdr on", "hdr off"):  # the all-screens HDR switch is "Windows HDR"
            self.assertTrue(titles(text)[0].startswith("Windows HDR"), text)

    def test_set_rows_show_current_value_bar(self) -> None:
        results = build_results(parse_query("c 40"), SCREENS, "sc")
        self.assertIsNone(results[0].progress_bar)  # "all" row: several values
        self.assertEqual([r.progress_bar.percentage for r in results[1:]], [70, 70])  # type: ignore[union-attr]

    def test_help(self) -> None:
        self.assertIs(parse_query("?").action, Action.HELP)
        self.assertIs(parse_query("help").action, Action.HELP)
        self.assertIs(parse_query("h").action, Action.HDR)  # "h" still means HDR
        t = titles("?")
        self.assertIn("sc hdr · sc hdr on · sc hdr off", t)
        self.assertTrue(any(x.startswith("Shift+Enter") for x in t))

    def test_overview_has_command_menu(self) -> None:
        t = titles("")
        for entry in ("Brightness", "Contrast", "HDR", "All commands"):
            self.assertIn(entry, t)

    def test_rows_acting_on_screens_have_a_submenu(self) -> None:
        for text in ("", "50", "c", "hdr", "id", "#1"):
            for r in build_results(parse_query(text), SCREENS, "sc"):
                acts = r.title and (r.title[0].isdigit() or r.title.startswith(("Set", "Turn", "Identify", "Brightness —", "Contrast —")))
                if acts and "unavailable" not in r.title:
                    self.assertTrue(getattr(r, "menu_screens", None), f"{text!r}: {r.title}")

    def test_quick_actions(self) -> None:
        from screen_control.menus import quick_actions

        one = [(r.title_tooltip if r.progress_bar else r.title) for r in quick_actions([SCREENS[0]])]
        self.assertEqual(one[0], "Brightness 0% — 1 · FQ320")
        self.assertIn("Contrast 75% — 1 · FQ320", one)
        self.assertIn("Turn HDR on — 1 · FQ320", one)
        self.assertEqual(one[-1], "Identify 1 · FQ320")
        all_titles = [(r.title_tooltip if r.progress_bar else r.title) for r in quick_actions(SCREENS)]
        self.assertIn("Brightness 50% — all 2 screens", all_titles)
        self.assertIn("Turn HDR off — all 2 screens", all_titles)  # one screen is on
        bars = [r.progress_bar.percentage for r in quick_actions([SCREENS[0]])[:5]]  # type: ignore[union-attr]
        self.assertEqual(bars, [0, 25, 50, 75, 100])

    def test_quick_actions_marks_current_value(self) -> None:
        from screen_control.menus import quick_actions

        titles_ = [(r.title_tooltip if r.progress_bar else r.title) for r in quick_actions([_screen(1, "A", "")])]  # brightness 40, contrast 70
        self.assertFalse(any("(current)" in x for x in titles_ if x.startswith("Brightness")))
        screen = _screen(1, "A", "")
        from dataclasses import replace
        screen = replace(screen, brightness=Level(50))
        self.assertIn("Brightness 50% — 1 · A   (current)", [(r.title_tooltip if r.progress_bar else r.title) for r in quick_actions([screen])])

    def test_no_text_is_drawn_over_progress_bars(self) -> None:
        from screen_control.menus import quick_actions

        rows = []
        for text in ("b", "c", "#1", "50", "c 40", "+10"):
            rows += build_results(parse_query(text), SCREENS, "sc")
        rows += quick_actions(SCREENS)
        with_bar = [r for r in rows if r.progress_bar is not None]
        self.assertTrue(with_bar)
        for r in with_bar:
            self.assertEqual(r.title, f"{r.progress_bar.percentage}%", r.title_tooltip)  # only the value
            self.assertTrue(r.sub)  # the text moved here
            self.assertTrue(r.title_tooltip)

    def test_action_rows_have_theme_glyphs_but_screens_keep_colour_icons(self) -> None:
        rows = {(r.title_tooltip if r.progress_bar else r.title): r for r in build_results(parse_query(""), SCREENS, "sc")}
        self.assertIsNotNone(rows["Brightness"].glyph)
        self.assertEqual(rows["Brightness"].glyph.font_family, "/Resources/#Segoe Fluent Icons")  # type: ignore[union-attr]
        self.assertIsNone(rows["1 · FQ320"].glyph)  # coloured number icon = overlay colour

    def test_long_subtitles_get_a_tooltip(self) -> None:
        for r in build_results(parse_query(""), SCREENS, "sc"):
            if r.sub and len(r.sub) > 72:
                self.assertEqual(r.sub_tooltip, r.sub)

    def test_half_typed_command_word_suggests_instead_of_error(self) -> None:
        self.assertEqual(titles("nig")[0], "Apply profile “night”")
        self.assertEqual(titles("mov")[0], "Apply profile “movie”")
        t = titles("n")  # name or night: screens that match, then both commands
        self.assertEqual(t[0], "1 · FQ320")
        self.assertIn("sc name", t)
        self.assertIn("sc night", t)
        self.assertIn("No screen matches", titles("dell")[0])  # a real non-matching screen word

    def test_no_ddc(self) -> None:
        screens = [_screen(1, "TV", "", ddc=False, hdr=None)]
        t = titles("50", screens)
        self.assertEqual(t, ["Brightness unavailable on 1 · TV"])

    def test_relative_preview(self) -> None:
        results = build_results(parse_query("#1 -50"), SCREENS, "sc")
        self.assertIn("40% → 0%", results[0].sub or "")

    def test_no_screens(self) -> None:
        self.assertEqual(titles("", []), ["No screens found"])


if __name__ == "__main__":
    unittest.main()
