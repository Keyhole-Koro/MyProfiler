"""Summarize a frame timeline: the Chrome trace `myemu --timeline` writes.

Each paint (the compositor's "paint" span) is one frame. For each the
summary gives its wall time, the guest instructions it ran and the 2D DMA
time inside it -- work the emulator does when the guest writes a DMA
command, which no instruction count shows -- and, for the slowest, the
spans it was made of. Then where the recording's time went: by guest stage,
by DMA command and on the emulator's side.
"""
import json
from collections import defaultdict
from pathlib import Path

GUEST, EMULATOR, SCREEN, DMA = 1, 2, 3, 4


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return [event for event in data["traceEvents"] if event.get("ph") != "M"]


def spans(events: list[dict]) -> list[dict]:
    """The guest's begin/end pairs as spans: name, start, end, instrs, depth."""
    out, stack = [], []
    for event in events:
        if event["tid"] != GUEST or event["ph"] not in "BE":
            continue
        if event["ph"] == "B":
            stack.append(event)
        elif stack:
            begin = stack.pop()
            out.append({"name": begin["name"], "start": begin["ts"], "end": event["ts"],
                        "instrs": event["args"]["instrs"] - begin["args"]["instrs"],
                        "depth": len(stack)})
    return out


def within(events, start, end, tid, name=None):
    return [e for e in events if e["tid"] == tid and start <= e["ts"] <= end
            and (name is None or e["name"] == name)]


def summarize(path: Path, top: int = 5) -> None:
    events = load(path)
    if not events:
        print("the timeline is empty")
        return
    length = (max(e["ts"] + e.get("dur", 0) for e in events) - min(e["ts"] for e in events)) / 1000
    guest = spans(events)
    paints = sorted((s for s in guest if s["name"] == "paint"), key=lambda s: s["start"])
    dma = [e for e in events if e["tid"] == DMA]
    swaps = sum(1 for e in events if e["tid"] == SCREEN and e["name"] == "swap")
    cursors = sum(1 for e in events if e["tid"] == SCREEN and e["name"] == "cursor")
    print(f"{path}: {length:.0f} ms recorded, {len(paints)} paints, {swaps} frames shown, "
          f"{cursors} cursor moves")

    rows = []
    for paint in paints:
        inside = [e for e in dma if paint["start"] <= e["ts"] <= paint["end"]]
        moves = within(events, paint["start"], paint["end"], SCREEN, "cursor")
        rows.append((paint, sum(e["dur"] for e in inside) / 1000, len(moves)))
    if rows:
        walls = sorted((p["end"] - p["start"]) / 1000 for p, _, _ in rows)
        print(f"paint: median {walls[len(walls) // 2]:.1f} ms, worst {walls[-1]:.1f} ms, "
              f"{sum(p['instrs'] for p, _, _ in rows) // len(rows):,} instrs and "
              f"{sum(d for _, d, _ in rows) / len(rows):.1f} ms of 2D DMA on average")
        print(f"\nslowest {min(top, len(rows))} paints:")
        for paint, dma_ms, moves in sorted(rows, key=lambda r: r[0]["start"] - r[0]["end"])[:top]:
            wall = (paint["end"] - paint["start"]) / 1000
            print(f"  at {paint['start'] / 1000:9.1f} ms: {wall:6.1f} ms, {paint['instrs']:>9,} instrs, "
                  f"2D DMA {dma_ms:5.1f} ms, cursor moved {moves}x meanwhile")
            parts = [s for s in guest if paint["start"] <= s["start"] and s["end"] <= paint["end"]
                     and s["depth"] > 0 and s is not paint]
            for part in sorted(parts, key=lambda s: s["start"] - s["end"])[:4]:
                print(f"      {(part['end'] - part['start']) / 1000:6.1f} ms  {part['instrs']:>9,} instrs  "
                      f"{part['name']}")

    stages = defaultdict(lambda: [0.0, 0, 0])
    for span in guest:
        stage = stages[span["name"]]
        stage[0] += (span["end"] - span["start"]) / 1000
        stage[1] += span["instrs"]
        stage[2] += 1
    print("\nguest spans (wall ms, instrs, count):")
    for name, (ms, instrs, count) in sorted(stages.items(), key=lambda kv: -kv[1][0])[:12]:
        print(f"  {ms:9.1f} ms  {instrs:>11,}  {count:5}  {name}")

    kinds = defaultdict(lambda: [0.0, 0])
    for event in dma:
        kind = event["name"].rsplit(" ", 1)[0]
        kinds[kind][0] += event["dur"] / 1000
        kinds[kind][1] += 1
    if kinds:
        print("\n2D DMA (host ms, commands):")
        for kind, (ms, count) in sorted(kinds.items(), key=lambda kv: -kv[1][0]):
            print(f"  {ms:9.1f} ms  {count:5}  {kind}")

    host = defaultdict(lambda: [0.0, 0])
    for event in events:
        if event["tid"] == EMULATOR or (event["tid"] == GUEST and event["ph"] == "X"):
            host[event["name"]][0] += event.get("dur", 0) / 1000
            host[event["name"]][1] += 1
    if host:
        print("\nemulator and idle (ms, count):")
        for name, (ms, count) in sorted(host.items(), key=lambda kv: -kv[1][0]):
            print(f"  {ms:9.1f} ms  {count:5}  {name}")
