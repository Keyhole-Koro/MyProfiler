"""Command line for MyProfiler."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import bench, compare, report, session
from .images import Image, ImageSet
from .profile import Profile, find_images

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(path: Path, args) -> Profile:
    if args.image:
        image = Image(args.image)
        return Profile(path, ImageSet([image], image.name), [])
    system, users = find_images(args.build)
    return Profile(path, system, users)


def run_system(args) -> int:
    """Boot the built system under the profiler."""
    disk = args.output.with_name(args.output.stem + ".disk.img")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.build / "disk.img", disk)
    myemu = REPO_ROOT / "runtime" / "MyEmulator" / "target" / "release" / "myemu"
    command = [str(myemu), "-i", str(args.build / "firmware_linked.mbin"), "--disk", str(disk),
               "--headless", "--timer-interval", "1000", "--step", str(args.steps),
               *session.emulator_arguments(args.build, args.output)]
    result = subprocess.run(command, capture_output=True, text=True)
    disk.unlink()
    if result.returncode != 0:
        print(result.stdout[-2000:], result.stderr[-2000:], file=sys.stderr)
        return result.returncode
    print(f"wrote {args.output}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="myprofiler", description="Read MyEmulator profiles.")
    commands = root.add_subparsers(dest="command", required=True)

    def with_images(command):
        command.add_argument("--build", type=Path, default=REPO_ROOT / "build",
                             help="directory with the system's images (default: build)")
        command.add_argument("--image", type=Path,
                             help="a single program's .mbin, instead of a system build")

    show = commands.add_parser("report", help="summarize a profile")
    show.add_argument("profile", type=Path)
    show.add_argument("--top", type=int, default=15)
    with_images(show)

    source = commands.add_parser("annotate", help="print a source file with its costs")
    source.add_argument("profile", type=Path)
    source.add_argument("file", help="end of the source path, like mm/mmu.unsafe.mln")
    with_images(source)

    diff = commands.add_parser("diff", help="compare two profiles by function")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--top", type=int, default=20)
    with_images(diff)

    boot = commands.add_parser("system", help="boot the system under the profiler")
    boot.add_argument("output", type=Path)
    boot.add_argument("--steps", type=int, default=200_000_000)
    boot.add_argument("--build", type=Path, default=REPO_ROOT / "build")

    measure = commands.add_parser("bench", help="record the e2e instruction table")
    measure.add_argument("output", type=Path)
    measure.add_argument("--keep", type=Path, help="keep each case's image and profile here")
    measure.add_argument("--jobs", type=int, default=os.cpu_count() or 1)

    table = commands.add_parser("bench-diff", help="compare two e2e tables")
    table.add_argument("before", type=Path)
    table.add_argument("after", type=Path)
    table.add_argument("--top", type=int, default=20)
    table.add_argument("--fail-on-regression", action="store_true",
                       help="exit 1 if any case got slower")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "report":
        report.report(load(args.profile, args), args.top)
    elif args.command == "annotate":
        return report.annotate(load(args.profile, args), args.file)
    elif args.command == "diff":
        compare.compare_profiles(load(args.before, args), load(args.after, args), args.top)
    elif args.command == "system":
        return run_system(args)
    elif args.command == "bench":
        return bench.record(REPO_ROOT, args.output, args.keep, args.jobs)
    elif args.command == "bench-diff":
        slower = compare.compare_tables(args.before, args.after, args.top)
        return 1 if slower and args.fail_on_regression else 0
    return 0
