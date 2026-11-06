"""Text reports over a Profile."""

from pathlib import Path

from .profile import Profile

# Addresses outside any image, named for the memory section.
REGIONS = [
    (0x1FFF0000, 0x20000000, "stack"),
    (0x24000000, 0x24001000, "MMIO"),
    (0x30000000, 0x30300000, "VRAM"),
]

COST_NOTES = {
    "": "ordinary code",
    None: "no line info",
    "frame": "prologue and epilogue",
    "copy": "aggregate copies",
    "check": "bounds checks",
    "arith": "multiply and divide loops",
    "drop": "drops",
}


def percent(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:5.1f}%" if whole else "  0.0%"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def short_path(path: str) -> str:
    parts = Path(path).parts
    return str(Path(*parts[-3:])) if len(parts) > 3 else path


def summary(profile: Profile) -> None:
    section(f"Programs (total={profile.total} instructions)")
    for name, count in sorted(profile.by_program().items(), key=lambda item: -item[1]):
        print(f"  {percent(count, profile.total)} {count:>13}  {name}")
    if profile.unknown_spaces:
        print(f"  {len(profile.unknown_spaces)} address space(s) matched no image in the build")
    threads = profile.data["threads"]
    if len(threads) > 1:
        section("Threads")
        for row in threads:
            name = {-1: "idle", -2: "boot"}.get(row["thread"], f"thread {row['thread']}")
            print(f"  {percent(row['instructions'], profile.total)} {row['instructions']:>13}  {name}")


def functions(profile: Profile, top: int) -> None:
    section("Functions (by self instructions)")
    print("   self%    incl%         self       calls  function")
    rows = sorted(profile.functions(), key=lambda row: -row.self_count)
    multiple = len(profile.by_program()) > 1
    for row in rows[:top]:
        where = f"{row.program}: " if multiple else ""
        print(f"  {percent(row.self_count, profile.total)}   {percent(row.inclusive, profile.total)}"
              f" {row.self_count:>12} {row.calls:>11}  {where}{row.name}")


def lines(profile: Profile, top: int) -> None:
    rows = sorted(profile.by_line().items(), key=lambda item: -item[1])
    if not rows:
        return
    section("Source lines")
    for (file, line), hits in rows[:top]:
        print(f"  {percent(hits, profile.total)} {hits:>13}  {short_path(file)}:{line}")


def costs(profile: Profile) -> None:
    rows = sorted(profile.by_cost().items(), key=lambda item: -item[1])
    if not any(kind for kind, _ in rows):
        return
    section("Cost kinds")
    for kind, hits in rows:
        print(f"  {percent(hits, profile.total)} {hits:>13}  {COST_NOTES.get(kind, kind)}")


def opcodes(profile: Profile, top: int) -> None:
    section("Opcodes")
    for row in profile.data["opcode_hits"][:top]:
        print(f"  {percent(row['count'], profile.total)} {row['count']:>13}  {row['mnemonic']}")


def memory_region(page: int) -> str:
    return next((name for low, high, name in REGIONS if low <= page < high), "")


def memory(profile: Profile, top: int) -> None:
    for key, title in (("mem_reads", "Memory reads"), ("mem_writes", "Memory writes")):
        rows = profile.data[key]
        total = sum(row["count"] for row in rows)
        section(f"{title} (by 4 KB page; total={total})")
        for row in rows[:top]:
            print(f"  {percent(row['count'], total)} {row['count']:>13}  "
                  f"0x{row['page']:08X} {memory_region(row['page'])}")


def edges(profile: Profile, top: int) -> None:
    section("Calls")
    for row in profile.data["edges"][:top]:
        caller = profile.label(row["caller_space"], row["caller"])
        callee = profile.label(row["callee_space"], row["callee"])
        print(f"  {row['count']:>11}  {caller} -> {callee}")


def report(profile: Profile, top: int) -> None:
    summary(profile)
    costs(profile)
    functions(profile, top)
    lines(profile, top)
    edges(profile, top)
    opcodes(profile, top)
    memory(profile, min(top, 5))


def annotate(profile: Profile, file_suffix: str) -> int:
    """Print a source file with the instructions each line retired."""
    file, counts = profile.line_costs(file_suffix)
    if file is None:
        print(f"no profiled source ends in {file_suffix}")
        return 1
    print(f"{file} ({sum(counts.values())} instructions, "
          f"{percent(sum(counts.values()), profile.total).strip()} of the profile)")
    source = Path(file).read_text(encoding="utf-8").splitlines()
    for number, text in enumerate(source, start=1):
        hits = counts.get(number)
        column = f"{hits:>12} {percent(hits, profile.total)}" if hits else " " * 19
        print(f"{column} {number:>5}  {text}")
    return 0
