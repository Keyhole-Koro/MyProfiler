# MyProfiler

Reads the profiles MyEmulator writes and says where the instructions went: by
function, source line, cost kind, process and thread. A separate command
records how many instructions the compiler's end-to-end programs take, so a
change can be compared against a baseline.

Profiles count retired instructions, not time. A program that runs to HALT
without a timer gives the same count every run. A booted system does not: the
timer runs on real time, so compare the end-to-end table, not two boots.

MyProfiler only reads. The measuring lives where the facts are:

| Fact | Owner |
|---|---|
| `.func`, `.loc` and `.cost` directives (`mlc --line-info`) | MyLangCompiler |
| The object's debug trailer | MyAssembler |
| `.map` and `.lines` next to an image | MyLinker |
| The profile JSON, `profile.reset` and `profile.write` | MyEmulator |

[docs/formats.md](docs/formats.md) describes the files between them.

## Commands

Run these from the MyComputer root after `make build`.

```bash
# Boot the system under the profiler, then read the result
python3 tools/MyProfiler/myprofiler.py system boot.json --steps 100000000
python3 tools/MyProfiler/myprofiler.py report boot.json

# One source file, with the instructions each line retired
python3 tools/MyProfiler/myprofiler.py annotate boot.json fs/fs.unsafe.mln

# What changed between two profiles, by function
python3 tools/MyProfiler/myprofiler.py diff before.json after.json

# The instruction table of the compiler's e2e programs
python3 tools/MyProfiler/myprofiler.py bench after.json
python3 tools/MyProfiler/myprofiler.py bench-diff qa/baselines/e2e-instructions.json after.json
```

For a single program, profile it with `myemu -i app.mbin --headless --profile
p.json` and pass `--image app.mbin` to `report`, `annotate` or `diff`.

## Measuring one interaction

The system runs only while a control request is being served, so a session can
measure exactly one interaction. `session.emulator_arguments` gives `myemu` the
flags that follow threads, and `tests/test_system.py` is a working example:

```python
args = session.emulator_arguments(build, output)
with MyOS.launch(firmware, disk=disk, extra_args=args) as os:
    os._request("profile.reset", {})
    ...                                    # the interaction to measure
    os._request("profile.write", {"path": str(output)})
```

## Limits

- The kernel's current-thread word is found by name in its map. Without it,
  `myemu --profile` keeps one call stack, which is only right for one thread.
- Cost kinds cover what the compiler marks. Reading a local back from the stack
  is ordinary code to the profiler, because the compiler has no such region.
- A generic's name loses its type arguments: `Text<>.len`.
- Instructions after the last `.func` in an object count toward that function.

## Tests

```bash
python3 tools/MyProfiler/tests/test_profiler.py
python3 tools/MyProfiler/tests/test_system.py    # needs make build
```
