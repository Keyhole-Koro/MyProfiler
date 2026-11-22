"""The UI benchmark: what the desktop's interactions cost, against a baseline.

Boots the built system over the automation bridge and profiles a few
interactions one at a time: dragging a window, typing in the Editor, moving
the pointer across windows. A booted system's totals move with the timer,
so the table keeps the costs a timer does not change: guest instructions per
painted frame (the compositor's present_frame), per pointer motion handled
(on_motion), per key dispatched, per hit test. The 2D DMA work behind a
frame is not in any instruction count; record a timeline for that
(`myemu --timeline`, summarized by `myprofiler.py frames`).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import session
from .profile import Profile, find_images

# metric -> (function whose inclusive cost per call it is, what it measures)
METRICS = {
    "paint_per_frame": ("present_frame", "guest instructions to paint one frame"),
    "motion_per_event": ("on_motion", "per pointer motion while dragging"),
    "key_per_event": ("dom_widgets_dispatch_key_event", "per key typed into the Editor"),
    "hit_test_per_call": ("dom_hit_test_find_at", "per hit test under the pointer"),
}
# Scenario -> the metrics it reports.
SCENARIOS = {
    "drag": ["paint_per_frame", "motion_per_event"],
    "type": ["paint_per_frame", "key_per_event"],
    "hover": ["hit_test_per_call"],
}
# A change smaller than this is noise: how pointer events coalesce moves
# a hit-test cost by about 1% between runs of the same build.
TOLERANCE = 0.02


def per_call(profile: Profile, suffix: str):
    calls = inclusive = 0
    for function in profile.functions():
        if function.name.endswith(suffix):
            calls += function.calls
            inclusive += function.inclusive
    return (inclusive // calls, calls) if calls else (None, 0)


def wait_for(locator, needle, timeout=30.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = locator.snapshot()
        if needle in (node.value or node.text):
            return
    raise AssertionError(f"{needle!r} never appeared")


def record(root: Path, output: Path) -> int:
    sys.path.insert(0, str(root / "system" / "MyOS" / "tests"))
    from mydomtester import MyOS, expect

    build = root / "build"
    system, users = find_images(build)
    table = {}
    with tempfile.TemporaryDirectory(prefix="ui-bench-", dir=build) as temporary:
        directory = Path(temporary)
        disk = directory / "disk.img"
        subprocess.run(["cp", "--sparse=always", str(build / "disk.img"), str(disk)], check=True)
        first = directory / "boot.json"
        with MyOS.launch(build / "firmware_linked.mbin", disk=disk,
                         extra_args=session.emulator_arguments(build, first)) as os:
            terminal = os.get_by_test_id("terminal")
            expect(terminal).to_be_visible()
            expect(os.get_by_role("window", name="Editor")).to_be_visible()
            wait_for(terminal, "$ ")

            def measure(name, action):
                path = directory / f"{name}.json"
                os._request("profile.reset", {})
                action()
                os._request("profile.write", {"path": str(path)})
                profile = Profile(path, system, users)
                row = {"instructions": profile.total}
                for metric in SCENARIOS[name]:
                    value, calls = per_call(profile, METRICS[metric][0])
                    row[metric] = value
                    row[metric + "_calls"] = calls
                table[name] = row
                print(f"{name}: " + ", ".join(f"{k} {v:,}" for k, v in row.items() if v is not None))

            # Boot's windows arrive in any order: put the Editor on top, so
            # every run hit-tests and paints the same stack.
            os.raise_window("Editor")

            # Over both windows: the hover changes as it crosses them.
            measure("hover", lambda: os._request("input.pointer.sequence", {"events": [
                {"type": "move", "x": 100 + i * 60, "y": 700} for i in range(31)]}))

            text = os.get_by_test_id("editor-text")
            text.click()
            measure("type", lambda: os._type("the quick brown fox jumps over"))

            editor = os.get_by_role("window", name="Editor").snapshot()
            box = editor.bounds
            grip = next((x, box.y + 16) for x in range(box.x + box.width // 2, box.x + 160, -20)
                        if os._hit_test(x, box.y + 16) == editor.id)

            def drag():
                x, y = grip
                events = [{"type": "move", "x": x, "y": y}, {"type": "down", "button": "left"}]
                events += [{"type": "move", "x": x + 10 * i, "y": y + 4 * i} for i in range(1, 31)]
                events += [{"type": "up", "button": "left"}]
                os._request("input.pointer.sequence", {"events": events})
            measure("drag", drag)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    print(f"wrote {output}")
    return 0


def compare(before: Path, after: Path) -> int:
    """Prints each metric's change; returns how many got slower."""
    old = json.loads(before.read_text())
    new = json.loads(after.read_text())
    slower = 0
    print(f"{'':28} {'before':>12} {'after':>12}  change")
    for scenario, metrics in SCENARIOS.items():
        for metric in metrics:
            a = old.get(scenario, {}).get(metric)
            b = new.get(scenario, {}).get(metric)
            if a is None or b is None:
                print(f"{scenario + '.' + metric:28} {str(a):>12} {str(b):>12}  (missing)")
                continue
            change = (b - a) / a if a else 0.0
            mark = ""
            if change > TOLERANCE:
                mark = "  slower"
                slower += 1
            elif change < -TOLERANCE:
                mark = "  faster"
            print(f"{scenario + '.' + metric:28} {a:>12,} {b:>12,}  {change * 100:+6.1f}%{mark}")
    return slower
