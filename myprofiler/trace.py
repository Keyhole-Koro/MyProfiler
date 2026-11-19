"""Record what really runs when an app sends one UI request.

Boots the built system under `myemu --control-stdio` with the profiler on,
waits for the desktop, focuses the Editor's text and starts a trace of the
functions in WATCH. Then it types one character. The Editor's handlers mark
the title changed and move the caret in the status line, and each of those
goes to the UI server as a SET_TEXT request. The driver polls the DOM until it
shows both, lets the Editor get its replies and go idle, and writes the trace.
Timer interrupts land in the same stretch, so the trace also shows how the
tick charges CPU time.

The guest runs only while a control request is served, so every wait here is
a DOM snapshot. docs/formats.md describes the file this writes.
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import session
from .mylinker import objformat

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "build" / "profile" / "trace.json"
DEFAULT_MYEMU = ROOT / "runtime" / "MyEmulator" / "target" / "release" / "myemu"
TITLE = "One UI request: a character typed into the Editor, and the timer ticks around it"
TIMER_INTERVAL_US = 1000  # myemu's default tick, given explicitly so the header can say it

# The functions to log, by program and by the name the source gives them:
# namespace.function, Type.method, or an assembly label.
WATCH = {
    "editor": [
        "runtime.next",                 # the event loop takes an event
        "runtime.call_handler",         # and runs the handler it was bound to
        "Editor.edited",
        "Editor.moved",
        "elements.Window_title",        # bound properties, called after every event
        "elements.Label_text",
        "elements.send_text",
        "runtime.send",
        "app_transport.request",
        "app_transport.poll",
        "app_transport.idle",
        "myos_service.call",
        "os_call",                      # the SYSCALL stub: OS_CALL(service, args)
        "sys_wait_notification",
    ],
    "kernel": [
        "irq_dispatch.dispatch",
        "keyboard.drain_hw",
        "syscall.dispatch",
        "os_call_router.handle",
        "request_gateway.request",
        "request_gateway.read_user_request",
        "request_gateway.copy_request_strings",
        "request_gateway.stage_request_outputs",
        "request_gateway.reply",
        "request_gateway.poll",
        "request_channel.submit",
        "request_channel.serve",
        "ipc.send",
        "ipc.recv",
        "scheduler.notify_armed",
        "scheduler.notify_pid",
        "scheduler.wait_notification",
        "scheduler.await_notification",
        "scheduler.resume_after_interrupt",
        "scheduler.tick",
        "scheduler.reschedule",
        "scheduler.charge_elapsed",
        "scheduler.charge",
        "process.charge_time",
        "dom_widgets.dispatch_key_event",
        "app_events.forward_ui_event",
        "app_events.push",
        "ui_protocol.handle",
        "ui_protocol.run_request",
        "ui_protocol.set_text",
    ],
}

# IRQ_CAUSE_* bits (runtime/MyEmulator/src/constants.rs, MyKernel irq_dispatch).
CAUSES = ["timer", "mouse", "serial", "ssd", "syscall", "page_fault",
          "privilege_violation", "keyboard", "illegal_instruction", "automation"]

PRIVATE_MARK = objformat.PRIVATE_LABEL_MARK  # MyLinker tags a private symbol `name-[module:n]`
NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)
TEXT_RANGE = re.compile(r"# text: 0x([0-9a-fA-F]+) \.\. 0x([0-9a-fA-F]+)")


# --- symbols ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Function:
    pc: int
    name: str    # as the source writes it
    symbol: str  # as the .map or .lines file writes it
    file: str    # the source, relative to the repository; "" for a label


_namespaces: dict[str, Optional[str]] = {}


def namespace_of(path: str) -> Optional[str]:
    if path not in _namespaces:
        try:
            match = NAMESPACE.search(Path(path).read_text(encoding="utf-8"))
        except OSError:
            match = None
        _namespaces[path] = match.group(1) if match else None
    return _namespaces[path]


def source_name(symbol: str, namespace: Optional[str]) -> str:
    """`request_channel_submit` in request_channel is request_channel.submit,
    a private `handle` in os_call_router is os_call_router.handle, and
    `Editor__edited` is the method Editor.edited."""
    if symbol.startswith("__mlg_"):
        return symbol  # an instantiated generic: its name carries its arguments
    if "__" in symbol.strip("_"):
        return symbol.replace("__", ".")
    if namespace is None:
        return symbol
    if symbol.startswith(namespace + "_"):
        return f"{namespace}.{symbol[len(namespace) + 1:]}"
    return f"{namespace}.{symbol}"


def relative(path: str) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return path


class Program:
    """The functions of one linked image, or of several sharing a space."""

    def __init__(self, name: str, images: list[Path]):
        self.name = name
        self.images = images
        self.functions: dict[int, Function] = {}
        self.ranges: list[tuple[int, int]] = []
        # The first image's bytes, to tell a running process by its text.
        self.data = images[0].read_bytes()
        for image in images:
            self._read(image)
        self.starts = sorted(self.functions)
        self.by_name: dict[str, list[Function]] = {}
        for function in self.functions.values():
            self.by_name.setdefault(function.name, []).append(function)

    def _read(self, image: Path) -> None:
        labels: dict[int, str] = {}
        text = (0, 0)
        map_path = image.with_name(image.name + ".map")
        for line in map_path.read_text(encoding="utf-8").splitlines():
            found = TEXT_RANGE.match(line)
            if found:
                text = (int(found.group(1), 16), int(found.group(2), 16))
            elif not line.startswith("#") and line.strip():
                address, _, symbol = line.partition(" ")
                labels[int(address, 16)] = symbol
        self.ranges.append(text)
        files: dict[str, str] = {}
        lines_path = image.with_name(image.name + ".lines")
        for line in lines_path.read_text(encoding="utf-8").splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "file":
                index, _, path = rest.partition(" ")
                files[index] = path
            elif kind == "func":
                address, _, rest = rest.partition(" ")
                index, _, symbol = rest.partition(" ")
                path = files[index]
                pc = int(address, 16)
                self.functions[pc] = Function(pc, source_name(symbol, namespace_of(path)),
                                              symbol, relative(path))
        # Assembly labels have no line table: the map names them.
        for pc, symbol in labels.items():
            if text[0] <= pc < text[1] and pc not in self.functions and PRIVATE_MARK not in symbol:
                self.functions[pc] = Function(pc, symbol, symbol, "")

    def find(self, name: str) -> Function:
        found = self.by_name.get(name, [])
        if len(found) == 1:
            return found[0]
        if found:
            raise SystemExit(f"{self.name}: {name} names {len(found)} functions")
        close = difflib.get_close_matches(name, self.by_name, n=5, cutoff=0.6)
        raise SystemExit(f"{self.name}: no function {name}; close: {', '.join(close) or 'none'}")

    def function_at(self, pc: int) -> Optional[Function]:
        if pc in self.functions:
            return self.functions[pc]
        if not any(low <= pc < high for low, high in self.ranges):
            return None
        index = bisect.bisect_right(self.starts, pc) - 1
        return self.functions[self.starts[index]] if index >= 0 else None

    def matches(self, text_base: int, sample: bytes) -> int:
        """How many bytes of a page sampled at `text_base` in a running process
        equal this program's text there, or 0 if any differ."""
        data = self.data
        entry, text_offset, text_size = (int.from_bytes(data[at:at + 4], "big") for at in (8, 12, 16))
        offset = text_base - (entry & ~0xFFF)
        if offset < 0 or offset >= text_size:
            return 0
        length = min(len(sample), text_size - offset)
        start = text_offset + offset
        return length if length >= 64 and data[start:start + length] == sample[:length] else 0


def load_programs(build: Path) -> dict[str, Program]:
    programs = {"kernel": Program("kernel", [build / "firmware_linked.mbin", build / "kernel_linked.mbin"])}
    for image in sorted((build / "user").glob("*.mbin")):
        if image.with_name(image.name + ".lines").exists():
            programs[image.stem] = Program(image.stem, [image])
    return programs


def thread_variable(build: Path) -> int:
    variable = session.thread_variable(build)
    if variable is None:
        raise SystemExit(f"{session.THREAD_SYMBOL} is not in the kernel's map")
    return variable


def contract_enum(path: Path, name: str) -> dict[int, str]:
    """`export enum Name { A = 1, B, ... }` from a contract, value -> name."""
    text = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
    body = re.search(r"enum\s+" + name + r"\s*\{(.*?)\}", text, re.DOTALL)
    values: dict[int, str] = {}
    if body is None:
        return values
    value = 0
    for entry in body.group(1).split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, _, explicit = entry.partition("=")
        if explicit.strip():
            value = int(explicit.strip(), 0)
        values[value] = key.strip()
        value += 1
    return values


# --- running -------------------------------------------------------------------------------------

def copy_build(build: Path, run: Path) -> Path:
    """The images and disk this run uses, so a rebuild meanwhile cannot change them."""
    for path in [*build.glob("*_linked.mbin*"), *(build / "user").glob("*.mbin*")]:
        target = run / path.relative_to(build)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    shutil.copyfile(build / "disk.img", run / "disk.img")
    return run


def watch_list(programs: dict[str, Program]) -> list[tuple[Program, Function]]:
    return [(programs[program], programs[program].find(name))
            for program, names in WATCH.items() for name in names]


def node(snapshot, test_id: str = "", role: str = "", name: str = ""):
    for item in snapshot.nodes:
        if ((test_id and item.test_id == test_id)
                or (role and item.role == role and item.name == name)):
            return item
    return None


class Follower:
    """Reads a running trace as it grows and tells when the Editor is idle again:
    every request it sent has returned and it is waiting for the next event."""

    def __init__(self, os, programs: dict[str, Program]):
        self.os = os
        self.programs = programs
        self.events: list[dict] = []
        self.next = 0
        self.spaces: dict[int, str] = {}

    def read(self) -> None:
        result = self.os._request("trace.read", {"since": self.next}, timeout=120)
        for space in result["spaces"]:
            if space["space"] not in self.spaces:
                self.spaces[space["space"]] = identify(space, self.programs)
        self.events += result["events"]
        self.next = result["next"]

    def editor_idle(self) -> bool:
        editor = self.programs["editor"]
        request = editor.find("app_transport.request").pc
        idle = editor.find("app_transport.idle").pc
        open_requests = finished = 0
        idle_after = False
        for event in self.events:
            if self.spaces.get(event["space"]) != "editor" or event["kind"] not in ("enter", "exit"):
                continue
            if event["pc"] == request:
                if event["kind"] == "enter":
                    open_requests += 1
                else:
                    open_requests -= 1
                    finished += 1
                idle_after = False
            elif event["pc"] == idle and event["kind"] == "enter":
                idle_after = True
        return finished > 0 and open_requests <= 0 and idle_after


def identify(space: dict, programs: dict[str, Program]) -> str:
    sample = bytes.fromhex(space["text_sample"])
    scores = {name: program.matches(space["text_base"], sample)
              for name, program in programs.items() if name != "kernel"}
    best = max(scores.values(), default=0)
    names = sorted(name for name, score in scores.items() if score and score == best)
    return "/".join(names) if names else f"space {space['space']:#x}"


def record(args, programs: dict[str, Program], run: Path) -> tuple[dict, dict]:
    sys.path.insert(0, str(ROOT / "system" / "MyOS" / "tests"))
    from mydomtester import MyOS, expect  # noqa: E402

    watched = watch_list(programs)
    watch = [{"pc": f.pc, "space": 0} if program.name == "kernel" else f.pc
             for program, f in watched]
    thread_variable(run)  # without it the trace cannot tell threads apart
    flags = [*session.emulator_arguments(run, run / "profile.json"),
             "--timer-interval", str(TIMER_INTERVAL_US)]
    with MyOS.launch(run / "firmware_linked.mbin", disk=run / "disk.img",
                     myemu_path=args.myemu, extra_args=flags) as os:
        # Boot's windows arrive in any order and each lands on top.
        expect(os.get_by_role("window", name="Editor")).to_be_visible()
        expect(os.get_by_role("window", name="Terminal")).to_be_visible()
        os.raise_window("Editor")
        text = os.get_by_test_id("editor-text")
        text.click()
        before = os.snapshot()
        settled = time.monotonic() + 2
        while time.monotonic() < settled:  # let the click's events play out
            after = os.snapshot()
            if after.revision == before.revision:
                break
            before = after
        old_text = node(before, test_id="editor-text").value
        old_position = node(before, test_id="editor-position").text
        old_title = node(before, role="window", name="Editor").text

        started = os._request("trace.start", {"watch": watch, "interrupts": True})
        os._request("input.key.type", {"text": args.char})
        deadline = time.monotonic() + args.timeout
        shown = None
        while time.monotonic() < deadline:
            snapshot = os.snapshot()
            value = node(snapshot, test_id="editor-text").value
            position = node(snapshot, test_id="editor-position").text
            title = node(snapshot, role="window", name="Editor").text
            if value != old_text and position != old_position and title.startswith("*"):
                shown = {"text": value, "title": title, "position": position}
                break
        if shown is None:
            raise SystemExit("the Editor never showed the typed character")
        follower = Follower(os, programs)
        follower.read()
        while not follower.editor_idle() and time.monotonic() < deadline:
            os.snapshot()
            follower.read()
        if not follower.editor_idle():
            print("warning: the Editor had not gone idle when the trace stopped", file=sys.stderr)
        os._request("trace.stop", {})
        raw_path = run / "trace.json"
        os._request("trace.write", {"path": str(raw_path)}, timeout=120)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    run_info = {
        "typed": args.char,
        "before": {"text": old_text, "title": old_title, "position": old_position},
        "after": shown,
        "watching": started["watching"],
    }
    return raw, run_info


# --- converting ------------------------------------------------------------------------------

def git_head(repo: Path) -> str:
    def git(*command: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *command], capture_output=True, text=True)
        return result.stdout.strip()
    head = git("rev-parse", "--short", "HEAD")
    return head + ("+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def build_info(build: Path, programs: dict[str, Program]) -> dict:
    images = {}
    for name in ("kernel", "editor"):
        for image in programs[name].images:
            stamp = image.with_name(image.name + ".stamp")
            built = datetime.datetime.fromtimestamp(image.stat().st_mtime).astimezone()
            images[image.name] = {
                "stamp": json.loads(stamp.read_text())["key"] if stamp.exists() else None,
                "built": built.isoformat(timespec="seconds"),
            }
    repos = ["", "system/MyKernel", "system/MyOS", "system/MyAppFramework",
             "toolchain/MyStdLib", "runtime/MyEmulator"]
    return {"images": images,
            "sources": {repo or "MyComputer": git_head(ROOT / repo) for repo in repos}}


def cause_names(bits: int) -> list[str]:
    return [name for bit, name in enumerate(CAUSES) if bits & (1 << bit)] or [hex(bits)]


def thread_labels(raw: dict, events: list[dict], programs: dict[str, Program],
                  spaces: dict[int, str]) -> dict[str, str]:
    """A user thread by its program; a kernel thread by the outermost `*.run`
    on its stack when the trace started, a frame's caller included: a thread
    entered through `iret` has no frame for its entry, but what it called
    returns there."""
    kernel = programs["kernel"]
    labels: dict[int, str] = {-1: "none running (idle)", -2: "boot"}
    users: dict[int, set[str]] = {}
    for event in events:
        if event["ev"] in ("enter", "exit") and event["program"] != "kernel":
            users.setdefault(event["thread"], set()).add(event["program"])
    for stack in raw["stacks"]:
        names = []
        for frame in stack["frames"]:
            if frame["space"]:
                users.setdefault(stack["thread"], set()).add(spaces.get(frame["space"], "user"))
                continue
            for pc in (frame["return"], frame["pc"]):
                function = kernel.function_at(pc) if pc is not None else None
                if function:
                    names.append(function.name)
        runs = [name for name in names if name.endswith(".run")]
        if runs:
            labels.setdefault(stack["thread"], "kernel: " + runs[0])
    for thread, names in users.items():
        labels[thread] = " / ".join(sorted(names))
    seen = {event["thread"] for event in events} | {event.get("from") for event in events}
    return {str(t): labels.get(t, "kernel thread") for t in sorted(t for t in seen if t is not None)}


def convert(raw: dict, programs: dict[str, Program], header: dict) -> dict:
    kernel = programs["kernel"]
    spaces = {space["space"]: identify(space, programs) for space in raw["spaces"]}
    watched = {(program, name) for program, names in WATCH.items() for name in names}
    syscalls = contract_enum(ROOT / "contracts" / "myos" / "syscall.contract.mln", "Syscall")
    services = contract_enum(ROOT / "contracts" / "myos" / "services.contract.mln", "OsService")
    started = raw["started"]["retired"]
    events: list[dict] = []
    for event in raw["events"]:
        kind = event["kind"]
        space = event["space"]
        program_name = "kernel" if space == 0 else spaces.get(space, f"space {space:#x}")
        out = {"seq": len(events), "clock": event["clock"], "thread": event["thread"]}
        if kind in ("enter", "exit"):
            program = programs.get(program_name)
            function = program.function_at(event["pc"]) if program else None
            name = function.name if function else f"{event['pc']:#010x}"
            if (program_name, name) not in watched:
                continue  # the same address in another program
            out.update(program=program_name, fn=name, ev=kind)
        elif kind in ("irq", "iret"):
            vector = kernel.function_at(event["pc"])
            out.update(program="kernel", fn=vector.name if vector else hex(event["pc"]), ev=kind)
            if kind == "irq":
                out["interrupted"] = program_name
                out["cause"] = cause_names(event["cause"])
                if event["trap"]:
                    out["trap"] = cause_names(event["trap"])[0]
                if "syscall" in event:
                    out["syscall"] = syscalls.get(event["syscall"], event["syscall"])
                    if out["syscall"] == "OS_CALL":
                        out["service"] = services.get(event["arg"], event["arg"])
        elif kind == "switch":
            out.update(program="kernel", fn=None, ev="switch")
            out["from"] = event["from"]
        else:
            out.update(program=None, fn=None, ev=kind, method=event.get("method"))
        if event.get("unwound"):
            out["unwound"] = True
        out["instr"] = event["retired"] - started
        events.append(out)
    header = dict(header)
    header["threads"] = thread_labels(raw, events, programs, spaces)
    header["run"]["events"] = len(events)
    header["run"]["truncated"] = raw["truncated"]
    return {**header, "events": events}


FIELDS = {
    "seq": "position in the trace",
    "clock": "the timer clock when it happened: ticks << 8 plus 1/256ths of the current tick, "
             "the unit scheduler.charge counts CPU time in",
    "thread": "the kernel's current-thread slot (g_current_thread_slot); -1 is none running",
    "program": "where the function is: kernel, or the user program whose address space it ran in",
    "fn": "the function, as the source names it",
    "ev": "enter, exit (a return; unwound: ended by a return further down the stack, as frames a "
          "thread switch left do), irq (an interrupt or trap vectored: cause, the trap, syscall "
          "and OS_CALL service, and the program it interrupted), iret (the interrupted code "
          "resumed), switch (the current thread changed, from the thread in 'from'), request "
          "(the driver's control request that let the guest run from here)",
    "instr": "instructions retired since the trace started",
}


def write(document: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = document.pop("events")
    with path.open("w", encoding="utf-8") as out:
        out.write("{\n")
        for key, value in document.items():
            out.write(f"  {json.dumps(key)}: {json.dumps(value, indent=2).replace(chr(10), chr(10) + '  ')},\n")
        out.write('  "events": [\n')
        for i, event in enumerate(events):
            out.write("    " + json.dumps(event) + (",\n" if i + 1 < len(events) else "\n"))
        out.write("  ]\n}\n")
    document["events"] = events


def summarize(document: dict) -> None:
    """The functions entered, in order, collapsing repeats."""
    rows: list[list] = []
    for event in document["events"]:
        if event["ev"] not in ("enter", "irq"):
            continue
        label = event["fn"] if event["ev"] == "enter" else (
            "IRQ " + "+".join(event["cause"]) + (f" {event['syscall']}" if "syscall" in event else "")
            + (f" {event['service']}" if "service" in event else ""))
        key = (event["thread"], event["program"] if event["ev"] == "enter" else "irq", label)
        if rows and rows[-1][0] == key:
            rows[-1][1] += 1
        else:
            rows.append([key, 1])
    for (thread, program, label), count in rows:
        print(f"  t{thread:<3} {program:<8} {label}" + (f" x{count}" if count > 1 else ""))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build", type=Path, default=ROOT / "build",
                        help="directory with the system's images (default: build)")
    parser.add_argument("--myemu", type=Path, default=DEFAULT_MYEMU, help="default: the release build")
    parser.add_argument("--char", default="x", help="the character to type (default: x)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: build/profile/trace.json")
    parser.add_argument("--raw", type=Path, help="also keep the emulator's own trace here")
    parser.add_argument("--convert", type=Path, metavar="RAW",
                        help="only convert a trace kept with --raw, with the images in --build")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for the Editor to show the character and go idle")
    parser.add_argument("--title", default=TITLE, help="what the run was, for whoever reads the trace")
    parser.add_argument("--quiet", action="store_true", help="do not print the sequence")


def main(args) -> int:
    if len(args.char) != 1:
        raise SystemExit("--char takes one character")

    date = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if args.convert:
        programs = load_programs(args.build)
        raw = json.loads(args.convert.read_text(encoding="utf-8"))
        run_info = {"converted_from": str(args.convert)}
        build = build_info(args.build, programs)
    else:
        with tempfile.TemporaryDirectory(prefix="myprofiler-trace-") as temporary:
            run = copy_build(args.build, Path(temporary))
            programs = load_programs(run)
            raw, run_info = record(args, programs, run)
            build = build_info(run, programs)
            if args.raw:
                args.raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(run / "trace.json", args.raw)

    header = {
        "format": 1,
        "title": args.title,
        "date": date,
        "build": build,
        "run": {**run_info, "timer_interval_us": TIMER_INTERVAL_US},
        "watched": [{"fn": f.name, "program": program.name, "symbol": f.symbol,
                     "pc": f"{f.pc:#010x}", "file": f.file}
                    for program, f in watch_list(programs)],
        "fields": FIELDS,
    }
    document = convert(raw, programs, header)
    write(document, args.out)
    print(f"wrote {relative(str(args.out.resolve()))} ({len(document['events'])} events)")
    if not args.quiet:
        summarize(document)
    return 0

