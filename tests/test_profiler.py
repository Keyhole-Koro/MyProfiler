#!/usr/bin/env python3
"""MyProfiler's reading of maps, line tables, profiles and benchmark tables."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myprofiler import compare, session  # noqa: E402
from myprofiler.bench import read_cases  # noqa: E402
from myprofiler.images import Image, ImageSet, short_name  # noqa: E402
from myprofiler.profile import Profile  # noqa: E402

TEXT = bytes(range(32))

MAP = """# MyLinker symbol map
# text: 0x20000 .. 0x20020 (32 bytes)
# data: 0x21000 .. 0x21004 (4 bytes)
0x00020000 __START__
0x00020008 Text__len
0x0002000C b_L_end_1-[app:3]
0x00021000 g_count
"""

LINES = """# MyLinker line table
file 0 /src/app.mln
func 0x00020000 0 main
func 0x00020010 0 helper
line 0x00020000 0 3 frame
line 0x00020008 0 4 -
line 0x00020010 0 9 copy
"""


def write_image(directory: Path, name: str, lines: bool = True) -> Path:
    image = directory / f"{name}.mbin"
    image.write_bytes(b"HEADER" + TEXT)
    (directory / f"{name}.mbin.map").write_text(MAP)
    if lines:
        (directory / f"{name}.mbin.lines").write_text(LINES)
    return image


def write_profile(directory: Path, name: str, hits: list, **fields) -> Path:
    data = {
        "format": 2,
        "total_instructions": sum(row["hits"] for row in hits),
        "spaces": [{"space": 0, "instructions": 0, "text_sample": ""}],
        "threads": [],
        "pc_hits": hits,
        "opcode_hits": [],
        "mem_reads": [],
        "mem_writes": [],
        "functions": [],
        "edges": [],
    }
    data.update(fields)
    path = directory / name
    path.write_text(json.dumps(data))
    return path


class Names(unittest.TestCase):
    def test_a_method_reads_like_the_source(self):
        self.assertEqual(short_name("Text__len"), "Text.len")

    def test_a_generic_keeps_its_base_and_method(self):
        self.assertEqual(short_name("__mlg_s_4_Text_c2_12__len"), "Text<>.len")
        self.assertEqual(short_name("__mlg_f_14_result_take_ok_p0_r0"), "result_take_ok<>")

    def test_a_reserved_name_is_left_alone(self):
        self.assertEqual(short_name("__START__"), "__START__")


class Images(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_the_line_table_names_functions_the_map_lacks(self):
        image = Image(write_image(self.directory, "app"))
        self.assertEqual(image.function(0x20004), "main")
        self.assertEqual(image.function(0x2000C), "Text.len")  # a block label does not split it
        self.assertEqual(image.function(0x20014), "helper")

    def test_without_a_line_table_the_map_still_names_functions(self):
        image = Image(write_image(self.directory, "app", lines=False))
        self.assertEqual(image.function(0x20004), "__START__")
        self.assertIsNone(image.position(0x20004))

    def test_a_row_covers_the_code_up_to_the_next(self):
        image = Image(write_image(self.directory, "app"))
        self.assertEqual(image.position(0x2000C).line, 4)
        self.assertEqual(image.position(0x20000).cost, "frame")
        self.assertEqual(image.position(0x20010).cost, "copy")

    def test_nothing_is_attributed_outside_the_text(self):
        image = Image(write_image(self.directory, "app"))
        self.assertIsNone(image.function(0x21000))
        self.assertIsNone(image.position(0x1FFFF))

    def test_a_sample_matches_only_its_own_program(self):
        image = Image(write_image(self.directory, "app"))
        padded = TEXT + bytes(100)  # the rest of the page is not part of the text
        self.assertTrue(image.matches(padded))
        self.assertFalse(image.matches(bytes(reversed(TEXT)) + bytes(100)))
        self.assertFalse(image.matches(b""))


class Profiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.image = Image(write_image(self.directory, "app"))
        self.system = ImageSet([], "kernel")

    def load(self, hits, spaces):
        path = write_profile(self.directory, "p.json", hits, spaces=spaces)
        return Profile(path, self.system, [self.image])

    def test_a_space_is_matched_to_its_program_by_text(self):
        hits = [{"space": 0x7000, "pc": 0x20004, "hits": 5}]
        spaces = [{"space": 0x7000, "instructions": 5, "text_sample": TEXT.hex()}]
        profile = self.load(hits, spaces)
        self.assertEqual(profile.by_function(), {("app", "main"): 5})
        self.assertEqual(profile.by_program(), {"app": 5})

    def test_a_space_without_a_program_is_reported_not_guessed(self):
        hits = [{"space": 0x7000, "pc": 0x20004, "hits": 5}]
        spaces = [{"space": 0x7000, "instructions": 5, "text_sample": "ff" * 64}]
        profile = self.load(hits, spaces)
        self.assertEqual(profile.unknown_spaces, [0x7000])
        self.assertEqual(profile.by_function(), {("space 0x7000", "0x00020004"): 5})

    def test_costs_and_lines_add_up_from_pcs(self):
        hits = [{"space": 0x7000, "pc": 0x20000, "hits": 2},
                {"space": 0x7000, "pc": 0x20008, "hits": 3},
                {"space": 0x7000, "pc": 0x20010, "hits": 4}]
        spaces = [{"space": 0x7000, "instructions": 9, "text_sample": TEXT.hex()}]
        profile = self.load(hits, spaces)
        self.assertEqual(profile.by_cost(), {"frame": 2, "": 3, "copy": 4})
        self.assertEqual(profile.by_line(), {("/src/app.mln", 3): 2, ("/src/app.mln", 4): 3,
                                             ("/src/app.mln", 9): 4})

    def test_another_format_is_refused(self):
        path = self.directory / "old.json"
        path.write_text(json.dumps({"total_instructions": 1}))
        with self.assertRaises(ValueError):
            Profile(path, self.system, [])


class Tables(unittest.TestCase):
    def test_a_slower_case_is_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            before = directory / "before.json"
            after = directory / "after.json"
            before.write_text(json.dumps({"cases": {"a": 10, "b": 10}}))
            after.write_text(json.dumps({"cases": {"a": 12, "b": 9}}))
            self.assertEqual(compare.compare_tables(before, after, 5), 1)

    def test_cases_are_read_with_their_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "e2e.cases"
            manifest.write_text("# comment\na|1|x.mln,y.mln\nb|2|z.mln|--error-trace\n")
            cases = read_cases(manifest)
        self.assertEqual([case.name for case in cases], ["a", "b"])
        self.assertEqual(cases[0].sources, ["x.mln", "y.mln"])
        self.assertEqual(cases[1].flags, ["--error-trace"])


class Session(unittest.TestCase):
    def test_the_thread_word_is_found_under_its_private_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "kernel_linked.mbin.map").write_text(
                "0x00100000 __START__\n0x001D7E14 g_current_thread_slot-[thread:800]\n")
            arguments = session.emulator_arguments(directory, directory / "o.json")
        self.assertEqual(arguments[2:], ["--profile-thread-var", "0x1d7e14"])


if __name__ == "__main__":
    unittest.main()
