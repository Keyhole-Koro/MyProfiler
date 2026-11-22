#!/usr/bin/env python3
"""MyProfiler's reading of maps, line tables, profiles and benchmark tables."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myprofiler import bench, cli, compare, frames, report, runs, session, trace, uibench  # noqa: E402
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


class Timelines(unittest.TestCase):
    """myprofiler.py frames reads the emulator's Chrome trace."""

    def test_a_paint_is_split_into_its_spans_and_its_dma(self):
        def event(tid, ph, name, ts, instrs=0, dur=None):
            e = {"name": name, "pid": 1, "tid": tid, "ph": ph, "ts": ts, "args": {"instrs": instrs}}
            if dur is not None:
                e["dur"] = dur
            return e
        trace = {"traceEvents": [
            {"name": "thread_name", "ph": "M", "pid": 1, "tid": 1, "args": {"name": "guest CPU"}},
            event(1, "B", "paint", 0, 100),
            event(1, "B", "Terminal", 1000, 150),
            event(4, "X", "dma shadow 1280x960", 2000, dur=5000),
            event(3, "i", "cursor", 3000),
            event(1, "E", "Terminal", 9000, 900),
            event(1, "E", "paint", 10000, 1100),
            event(3, "i", "swap", 10500),
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.json"
            path.write_text(json.dumps(trace))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                frames.summarize(path, top=1)
        text = out.getvalue()
        self.assertIn("1 paints, 1 frames shown, 1 cursor moves", text)
        self.assertIn("10.0 ms,     1,000 instrs, 2D DMA   5.0 ms, cursor moved 1x", text)
        self.assertIn("750 instrs  Terminal", text)
        self.assertIn("dma shadow", text)


class UiBench(unittest.TestCase):
    """myprofiler.py ui-bench-diff compares two tables."""

    def test_only_a_change_past_the_tolerance_counts_as_slower(self):
        before = {"drag": {"paint_per_frame": 1000, "motion_per_event": 100},
                  "type": {"paint_per_frame": 1000, "key_per_event": 100},
                  "hover": {"hit_test_per_call": 100}}
        after = json.loads(json.dumps(before))
        after["drag"]["paint_per_frame"] = 1015   # +1.5%: noise
        after["type"]["key_per_event"] = 110      # +10%: slower
        after["hover"]["hit_test_per_call"] = 50  # faster
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.json", Path(directory) / "b.json"
            a.write_text(json.dumps(before))
            b.write_text(json.dumps(after))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                slower = uibench.compare(a, b)
        self.assertEqual(slower, 1)
        self.assertIn("type.key_per_event", out.getvalue())
        self.assertIn("faster", out.getvalue())


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

    def test_an_image_without_a_text_range_matches_nothing(self):
        bare = write_image(self.directory, "bare", lines=False)
        (self.directory / "bare.mbin.map").unlink()
        self.assertFalse(Image(bare).matches(TEXT + bytes(100)))
        empty = write_image(self.directory, "empty")
        (self.directory / "empty.mbin.map").write_text(MAP.replace("0x20020 (32", "0x20000 (0"))
        self.assertFalse(Image(empty).matches(TEXT + bytes(100)))


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


class Reports(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def load(self, source: Path, hits: list, **fields) -> Profile:
        image = write_image(self.directory, "app")
        (self.directory / "app.mbin.lines").write_text(LINES.replace("/src/app.mln", str(source)))
        spaces = [{"space": 0x7000, "instructions": sum(row["hits"] for row in hits),
                   "text_sample": TEXT.hex()}]
        path = write_profile(self.directory, "p.json", hits, spaces=spaces, **fields)
        return Profile(path, ImageSet([], "kernel"), [Image(image)])

    def printed(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = function(*args)
        return result, out.getvalue()

    def test_a_source_that_is_gone_is_reported_not_raised(self):
        gone = self.directory / "moved" / "app.mln"
        profile = self.load(gone, [{"space": 0x7000, "pc": 0x20000, "hits": 2}])
        result, out = self.printed(report.annotate, profile, "app.mln")
        self.assertEqual(result, 1)
        self.assertIn(str(gone), out)

    def test_a_report_prints_every_section_it_has_data_for(self):
        source = self.directory / "app.mln"
        hits = [{"space": 0x7000, "pc": 0x20000, "hits": 2},
                {"space": 0x7000, "pc": 0x20010, "hits": 6}]
        profile = self.load(
            source, hits,
            threads=[{"thread": -2, "instructions": 2}, {"thread": 3, "instructions": 6}],
            functions=[{"space": 0x7000, "entry": 0x20010, "self": 6, "inclusive": 6, "calls": 1}],
            edges=[{"caller_space": 0x7000, "caller": 0x20000,
                    "callee_space": 0x7000, "callee": 0x20010, "count": 1}],
            opcode_hits=[{"mnemonic": "ADD", "count": 8}],
            mem_reads=[{"page": 0x30000000, "count": 4}],
            mem_writes=[{"page": 0x00021000, "count": 1}])
        _, out = self.printed(report.report, profile, 5)
        for title in ("Programs (total=8", "Threads", "Cost kinds", "Functions", "Source lines",
                      "Calls", "Opcodes", "Memory reads", "Memory writes"):
            self.assertIn(f"=== {title}", out)
        self.assertIn("boot", out)
        self.assertIn("thread 3", out)
        self.assertIn("aggregate copies", out)
        self.assertIn("main -> helper", out)
        self.assertIn("0x30000000 VRAM", out)
        self.assertIn(f"  {report.short_path(str(source))}:9\n", out)

    def test_annotate_reads_the_matching_file_with_the_most_hits(self):
        quiet = self.directory / "a" / "app.mln"
        busy = self.directory / "b" / "app.mln"
        for path in (quiet, busy):
            path.parent.mkdir()
            path.write_text("\n".join(f"line {n}" for n in range(1, 10)) + "\n")
        image = write_image(self.directory, "app")
        (self.directory / "app.mbin.lines").write_text(
            f"file 0 {quiet}\nfile 1 {busy}\nfunc 0x00020000 0 main\n"
            "line 0x00020000 0 3 -\nline 0x00020010 1 9 -\n")
        hits = [{"space": 0x7000, "pc": 0x20000, "hits": 2},
                {"space": 0x7000, "pc": 0x20010, "hits": 6}]
        spaces = [{"space": 0x7000, "instructions": 8, "text_sample": TEXT.hex()}]
        path = write_profile(self.directory, "p.json", hits, spaces=spaces)
        profile = Profile(path, ImageSet([], "kernel"), [Image(image)])
        result, out = self.printed(report.annotate, profile, "app.mln")
        self.assertEqual(result, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{busy} (6 instructions, 75.0% of the profile)")
        self.assertRegex(lines[9], r"^\s+6\s+75\.0%\s+9  line 9$")
        self.assertRegex(lines[3], r"^\s{19}\s+3  line 3$")

    def test_annotate_says_when_no_source_matches(self):
        profile = self.load(self.directory / "app.mln", [{"space": 0x7000, "pc": 0x20000, "hits": 1}])
        result, out = self.printed(report.annotate, profile, "other.mln")
        self.assertEqual(result, 1)
        self.assertIn("no profiled source ends in other.mln", out)

    def test_memory_regions_and_an_empty_whole(self):
        self.assertEqual(report.memory_region(0x1FFF0000), "stack")
        self.assertEqual(report.memory_region(0x24000FFF), "MMIO")
        self.assertEqual(report.memory_region(0x30300000), "")
        self.assertEqual(report.percent(1, 0), "  0.0%")
        self.assertEqual(report.percent(1, 4), " 25.0%")

    def test_a_profile_diff_marks_new_functions_and_stops_at_unchanged_ones(self):
        source = self.directory / "app.mln"
        before = self.load(source, [{"space": 0x7000, "pc": 0x20000, "hits": 5}])
        after = self.load(source, [{"space": 0x7000, "pc": 0x20000, "hits": 5},
                                   {"space": 0x7000, "pc": 0x20010, "hits": 3}])
        _, out = self.printed(compare.compare_profiles, before, after, 10)
        self.assertTrue(out.startswith("total:"))
        self.assertRegex(out, r"\s0\s+3\s+\+3\s+new\s+app: helper")
        self.assertNotIn("app: main", out)


class Tables(unittest.TestCase):
    def test_a_slower_case_is_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            before = directory / "before.json"
            after = directory / "after.json"
            before.write_text(json.dumps({"cases": {"a": 10, "b": 10}}))
            after.write_text(json.dumps({"cases": {"a": 12, "b": 9}}))
            self.assertEqual(compare.compare_tables(before, after, 5), 1)

    def test_bench_diff_fails_only_when_asked_and_a_case_is_slower(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            before = directory / "before.json"
            slower = directory / "slower.json"
            faster = directory / "faster.json"
            before.write_text(json.dumps({"cases": {"a": 10}}))
            slower.write_text(json.dumps({"cases": {"a": 11}}))
            faster.write_text(json.dumps({"cases": {"a": 9}}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["bench-diff", str(before), str(slower)]), 0)
                self.assertEqual(cli.main(["bench-diff", str(before), str(slower),
                                           "--fail-on-regression"]), 1)
                self.assertEqual(cli.main(["bench-diff", str(before), str(faster),
                                           "--fail-on-regression"]), 0)

    def test_a_failed_case_leaves_the_table_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            compiler = repo / "toolchain" / "MyLangCompiler"
            (compiler / "tests").mkdir(parents=True)
            (compiler / "tests" / "e2e.cases").write_text("a|1|x.mln\n")
            (compiler / "mlc").write_text("#!/bin/sh\nexit 1\n")
            (compiler / "mlc").chmod(0o755)
            table = repo / "baseline.json"
            table.write_text("kept")
            self.assertEqual(bench.record(repo, table, None, 1), 1)
            self.assertEqual(table.read_text(), "kept")

    def test_cases_are_read_with_their_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "e2e.cases"
            manifest.write_text("# comment\na|1|x.mln,y.mln\nb|2|z.mln|--error-trace\n")
            cases = read_cases(manifest)
        self.assertEqual([case.name for case in cases], ["a", "b"])
        self.assertEqual(cases[0].sources, ["x.mln", "y.mln"])
        self.assertEqual(cases[1].flags, ["--error-trace"])


class Runs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name)
        (self.build / "user").mkdir()

    def record(self, text: str) -> Path:
        write_image(self.build, "kernel_linked")
        (self.build / "kernel_linked.mbin.map").write_text(MAP.replace("Text__len", text))
        write_image(self.build / "user", "hello")
        runs.start(self.build).write_text(text)
        return runs.finish(self.build)

    def test_a_new_run_moves_the_last_one_back(self):
        self.record("first")
        last = self.record("second")
        self.assertEqual(last.read_text(), "second")
        self.assertEqual(runs.profile_of(self.build, "previous").read_text(), "first")
        self.assertFalse((self.build / "profile" / "next").exists())

    def test_a_run_reads_the_images_it_ran_not_the_current_build(self):
        self.record("first")
        self.record("second")
        previous = runs.profile_of(self.build, "previous")
        directory = runs.image_directory(previous, self.build)
        self.assertEqual(directory, previous.parent)
        self.assertEqual(Image(directory / "kernel_linked.mbin").function(0x20008), "first")
        self.assertTrue((directory / "user" / "hello.mbin").exists())

    def test_a_loose_profile_reads_the_build(self):
        self.assertEqual(runs.image_directory(self.build / "x" / "p.json", self.build), self.build)

    def test_an_unfinished_run_does_not_replace_the_last(self):
        last = self.record("first")
        runs.start(self.build).write_text("interrupted")
        self.assertEqual(last.read_text(), "first")

    def test_a_missing_profile_says_how_to_get_one(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["report", "--build", str(self.build)])
        self.assertIn("make profile", str(caught.exception))

    def test_a_boot_that_cannot_start_leaves_no_disk_copy(self):
        (self.build / "disk.img").write_bytes(b"disk")
        output = self.build / "out" / "p.json"
        original = cli.REPO_ROOT
        cli.REPO_ROOT = self.build  # no runtime/MyEmulator here
        self.addCleanup(setattr, cli, "REPO_ROOT", original)
        with self.assertRaises(FileNotFoundError):
            cli.main(["system", str(output), "--build", str(self.build)])
        self.assertFalse((output.parent / "p.disk.img").exists())


class Session(unittest.TestCase):
    def test_the_thread_word_is_found_under_its_private_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "kernel_linked.mbin.map").write_text(
                "0x00100000 __START__\n0x001D7E14 g_current_thread_slot-[thread:800]\n")
            arguments = session.emulator_arguments(directory, directory / "o.json")
        self.assertEqual(arguments[2:], ["--profile-thread-var", "0x1d7e14"])



class TraceTest(unittest.TestCase):
    def test_names_read_as_the_source_writes_them(self):
        self.assertEqual(trace.source_name("request_channel_submit", "request_channel"), "request_channel.submit")
        self.assertEqual(trace.source_name("handle", "os_call_router"), "os_call_router.handle")
        self.assertEqual(trace.source_name("Editor__edited", "editor"), "Editor.edited")
        self.assertEqual(trace.source_name("os_call", None), "os_call")

    def test_interrupt_causes_by_bit(self):
        self.assertEqual(trace.cause_names(0b10001), ["timer", "syscall"])
        self.assertEqual(trace.cause_names(0), ["0x0"])

    def test_contract_enum_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "services.contract.mln"
            path.write_text("export enum OsService {\n    NONE,\n    REQUEST, // apps\n    LOG = 20,\n    MORE\n}\n")
            self.assertEqual(trace.contract_enum(path, "OsService"), {0: "NONE", 1: "REQUEST", 20: "LOG", 21: "MORE"})


PROGRAM_TEXT = bytes(range(128))


def write_program(directory: Path, name: str, text: bytes = PROGRAM_TEXT) -> Path:
    """An image laid out as `trace.Program` reads it: entry, text offset and
    text size at bytes 8, 12 and 16, then a map and a line table."""
    image = directory / f"{name}.mbin"
    header = b"MBIN" + bytes(4) + (0x20000).to_bytes(4, "big") + (32).to_bytes(4, "big")
    header += len(text).to_bytes(4, "big")
    image.write_bytes(header.ljust(32, b"\0") + text)
    (directory / f"{name}.mbin.map").write_text(
        f"# text: 0x20000 .. 0x{0x20000 + len(text):05X}\n"
        "0x00020000 __START__\n0x00020040 isr_entry\n0x00020050 b_L_end_1-[app:3]\n")
    (directory / f"{name}.mbin.lines").write_text(
        f"file 0 {directory / name}.mln\n"
        "func 0x00020010 0 Editor__edited\nfunc 0x00020020 0 helper\nfunc 0x00020030 0 helper\n"
        "line 0x00020010 0 3 -\n")
    return image


class TracePrograms(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.program = trace.Program("app", [write_program(self.directory, "app")])

    def test_a_pc_belongs_to_the_function_before_it_within_the_text(self):
        names = {pc: self.program.function_at(pc) for pc in (0x20000, 0x20014, 0x20044, 0x20054)}
        self.assertEqual({pc: f.name for pc, f in names.items()},
                         {0x20000: "__START__", 0x20014: "Editor.edited",
                          0x20044: "isr_entry", 0x20054: "isr_entry"})  # a private label does not split it
        self.assertEqual(names[0x20044].file, "")  # an assembly label has no source
        self.assertIsNone(self.program.function_at(0x20080))
        self.assertIsNone(self.program.function_at(0x1FFFC))

    def test_a_name_is_found_only_when_it_names_one_function(self):
        self.assertEqual(self.program.find("Editor.edited").pc, 0x20010)
        with self.assertRaises(SystemExit) as caught:
            self.program.find("helper")
        self.assertIn("names 2 functions", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.program.find("helpr")
        self.assertIn("close: helper", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            self.program.find("zzz")
        self.assertIn("close: none", str(caught.exception))

    def test_a_page_matches_only_with_enough_equal_text(self):
        self.assertEqual(self.program.matches(0x20000, PROGRAM_TEXT + bytes(100)), 128)
        self.assertEqual(self.program.matches(0x20000, PROGRAM_TEXT[:63]), 0)
        self.assertEqual(self.program.matches(0x20000, PROGRAM_TEXT[:64]), 64)
        self.assertEqual(self.program.matches(0x20000, bytes(reversed(PROGRAM_TEXT))), 0)
        self.assertEqual(self.program.matches(0x21000, PROGRAM_TEXT), 0)  # past the text
        self.assertEqual(self.program.matches(0x1F000, PROGRAM_TEXT), 0)  # before it

    def test_a_space_is_named_by_the_best_matching_user_program(self):
        other = trace.Program("other", [write_program(self.directory, "other", bytes(128))])
        programs = {"kernel": self.program, "app": self.program, "other": other}
        space = {"space": 0x7000, "text_base": 0x20000, "text_sample": PROGRAM_TEXT.hex()}
        self.assertEqual(trace.identify(space, programs), "app")
        self.assertEqual(trace.identify(space, {"kernel": self.program, "other": other}), "space 0x7000")
        twin = {"app": self.program, "twin": self.program}
        self.assertEqual(trace.identify(space, twin), "app/twin")

    def test_a_written_trace_is_json_and_the_document_keeps_its_events(self):
        events = [{"seq": 0, "ev": "enter"}, {"seq": 1, "ev": "exit"}]
        document = {"format": 1, "run": {"typed": "x", "nested": [1, 2]}, "events": events}
        path = self.directory / "out" / "trace.json"
        trace.write(document, path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"format": 1, "run": {"typed": "x", "nested": [1, 2]}, "events": events})
        self.assertIs(document["events"], events)
        empty = {"format": 1, "events": []}
        trace.write(empty, path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"format": 1, "events": []})

    def test_a_summary_folds_repeated_entries(self):
        enter = {"ev": "enter", "thread": 2, "program": "editor", "fn": "runtime.next"}
        events = [enter, enter, {**enter, "ev": "exit"}, enter,
                  {"ev": "irq", "thread": 2, "program": "kernel", "fn": "isr", "cause": ["syscall"],
                   "syscall": "OS_CALL", "service": "REQUEST"},
                  {**enter, "thread": 5}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            trace.summarize({"events": events})
        self.assertEqual(out.getvalue().splitlines(), [
            "  t2   editor   runtime.next x3",
            "  t2   irq      IRQ syscall OS_CALL REQUEST",
            "  t5   editor   runtime.next",
        ])


if __name__ == "__main__":
    unittest.main()
