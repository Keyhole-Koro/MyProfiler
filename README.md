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

Run these from the MyComputer root. They rebuild what they measure.

```bash
# Profile the boot and report on it
make profile
make profile TOP=40

# One source file from that profile, with the instructions each line retired
make profile SOURCE=fs/fs.unsafe.mln

# What changed between the last two boots, by function
make profile-diff

# The compiler's e2e programs against the baseline, and accepting a new one
make bench
make bench-save
```

`build/profile` holds the results: `last` and `previous` are the last two boots,
each with a copy of the images it ran, and `bench.json` is the last e2e table.
A profile holds addresses and the next build moves them, so reading `previous`
through the current build would name the wrong functions. Copy a run elsewhere
under `build/profile` to keep it longer.

`make profile` measures the boot up to the first time the CPU waits for an
interrupt, about 35 million instructions, all in the kernel. To measure a user
program, see [Measuring one interaction](#measuring-one-interaction).

Where the timer lands changes the boot slightly: two runs of one build have
differed by 1.4%. `make profile-diff` shows that difference too, so use
`make bench` for small changes.

`myprofiler.py` takes the same defaults, and paths when you want others:

```bash
python3 tools/MyProfiler/myprofiler.py report --top 40
python3 tools/MyProfiler/myprofiler.py annotate mm/mmu.unsafe.mln build/profile/previous/profile.json
python3 tools/MyProfiler/myprofiler.py diff build/profile/keep/profile.json
python3 tools/MyProfiler/myprofiler.py bench-diff --fail-on-regression
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
