#!/usr/bin/env python3
"""Profile a running system: a user process must show up under its own name.

Needs a built system (`make build`). Drives the terminal over the automation
bridge, measures only that stretch, and reads the result back with MyProfiler.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "MyProfiler"))
sys.path.insert(0, str(ROOT / "system" / "MyOS" / "tests"))

from mydomtester import MyOS, expect  # noqa: E402
from myprofiler import session  # noqa: E402
from myprofiler.profile import Profile, find_images  # noqa: E402


def wait_for(locator, needle, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = locator.snapshot()
        if needle in (node.value or node.text):
            return
    raise AssertionError(f"{needle!r} never appeared")


def main() -> int:
    build = ROOT / "build"
    with tempfile.TemporaryDirectory(prefix="myprofiler-system-") as temporary:
        directory = Path(temporary)
        disk = directory / "disk.img"
        shutil.copyfile(build / "disk.img", disk)
        output = directory / "session.json"
        with MyOS.launch(build / "firmware_linked.mbin", disk=disk,
                         extra_args=session.emulator_arguments(build, output)) as os:
            terminal = os.get_by_test_id("terminal")
            # Boot's windows arrive in any order and each lands on top, so
            # wait for them and bring the terminal back to the front, once
            # its shell is ready.
            expect(terminal).to_be_visible()
            expect(os.get_by_role("window", name="Notes")).to_be_visible()
            wait_for(terminal, "$ ")
            os.raise_window("Terminal")
            os._request("profile.reset", {})
            terminal.click()
            os._type("hello")
            os._press("enter")
            wait_for(terminal, "exited with 7")
            os._request("profile.write", {"path": str(output)})

        system, users = find_images(build)
        profile = Profile(output, system, users)
        programs = profile.by_program()
        for name, count in sorted(programs.items(), key=lambda item: -item[1]):
            print(f"{count:>12}  {name}")
        print("threads:", len(profile.data["threads"]))

        failures = []
        if profile.unknown_spaces:
            failures.append(f"unmatched address spaces: {profile.unknown_spaces}")
        if programs.get("hello", 0) == 0:
            failures.append("hello did not appear as a program")
        if len(profile.data["threads"]) < 2:
            failures.append("expected more than one thread")
        hello = {name for program, name in profile.by_function() if program == "hello"}
        if "app_main" not in hello and "main" not in hello:
            failures.append(f"hello's functions did not resolve: {sorted(hello)[:8]}")
        unresolved = sum(hits for (_, name), hits in profile.by_function().items()
                         if name.startswith("0x"))
        if unresolved * 100 > profile.total:
            failures.append(f"{unresolved} of {profile.total} instructions have no function")
        if failures:
            print("FAIL: " + "; ".join(failures))
            return 1
    print("PASS: the session names its processes and threads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
