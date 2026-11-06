"""What changed between two profiles, or two benchmark tables."""

import json
from pathlib import Path

from .profile import Profile


def delta_text(before: int, after: int) -> str:
    change = after - before
    relative = f"{100.0 * change / before:+7.1f}%" if before else "    new "
    return f"{before:>13} {after:>13} {change:>+12} {relative}"


def compare_profiles(before: Profile, after: Profile, top: int) -> None:
    print(f"total: {delta_text(before.total, after.total)}")
    old = before.by_function()
    new = after.by_function()
    keys = set(old) | set(new)
    rows = sorted(keys, key=lambda key: -abs(new.get(key, 0) - old.get(key, 0)))
    print("\n       before         after       change         %  function")
    for key in rows[:top]:
        if new.get(key, 0) == old.get(key, 0):
            break
        program, name = key
        print(f"{delta_text(old.get(key, 0), new.get(key, 0))}  {program}: {name}")


def compare_tables(before_path: Path, after_path: Path, top: int) -> int:
    """Compare two `bench` tables. Returns the number of cases that got slower."""
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))["cases"]
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))["cases"]
    shared = sorted(set(before) & set(after))
    old_total = sum(before[name] for name in shared)
    new_total = sum(after[name] for name in shared)
    print(f"{len(shared)} cases: {delta_text(old_total, new_total)}")
    for side, names in (("only before", set(before) - set(after)), ("only after", set(after) - set(before))):
        if names:
            print(f"{side}: {', '.join(sorted(names))}")
    changed = [name for name in shared if before[name] != after[name]]
    changed.sort(key=lambda name: -abs(after[name] - before[name]))
    if changed:
        print("\n       before         after       change         %  case")
    for name in changed[:top]:
        print(f"{delta_text(before[name], after[name])}  {name}")
    return sum(1 for name in shared if after[name] > before[name])
