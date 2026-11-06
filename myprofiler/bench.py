"""Instruction counts of the compiler's end-to-end programs.

Each case in `tests/e2e.cases` runs to HALT without a timer, so its count
repeats exactly. That makes the table a baseline: change the compiler, record
again and compare. Cases build the way MyLangTester builds them, plus line info.
"""

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Case:
    name: str
    sources: list[str]
    flags: list[str]


def read_cases(manifest: Path) -> list[Case]:
    cases = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("|")
        flags = fields[3].split() if len(fields) > 3 else []
        cases.append(Case(fields[0], fields[2].split(","), flags))
    return cases


class Toolchain:
    def __init__(self, repo: Path):
        self.compiler_dir = repo / "toolchain" / "MyLangCompiler"
        self.tests = self.compiler_dir / "tests"
        self.mlc = self.compiler_dir / "mlc"
        self.myas = repo / "toolchain" / "MyAssembler" / "build" / "myas"
        self.linker = repo / "toolchain" / "MyLinker" / "mllinker"
        self.myemu = repo / "runtime" / "MyEmulator" / "target" / "release" / "myemu"

    def build(self, case: Case, directory: Path) -> Path:
        objects = []
        for index, source in enumerate(case.sources):
            assembly = directory / f"{index}.masm"
            obj = directory / f"{index}.mobj"
            run([self.mlc, "--line-info", *case.flags, self.tests / source, assembly], directory)
            run([self.myas, assembly, directory / f"{index}.mbin", "--obj", obj], directory)
            objects.append(obj)
        image = directory / f"{case.name}.mbin"
        run([self.linker, "--map", f"{image}.map", "--lines", f"{image}.lines", image, *objects], directory)
        return image

    def profile(self, image: Path, output: Path) -> None:
        run([self.myemu, "-i", image, "--headless", "--profile", output], image.parent)


def run(command: list, directory: Path) -> None:
    subprocess.run([str(part) for part in command], cwd=directory, check=True,
                   capture_output=True, text=True)


def measure(tools: Toolchain, case: Case, root: Path) -> tuple[str, Optional[int], str]:
    directory = root / case.name
    directory.mkdir(parents=True, exist_ok=True)
    try:
        image = tools.build(case, directory)
        output = directory / "profile.json"
        tools.profile(image, output)
    except subprocess.CalledProcessError as error:
        return case.name, None, (error.stderr or error.stdout or str(error)).strip()
    data = json.loads(output.read_text(encoding="utf-8"))
    return case.name, data["total_instructions"], ""


def record(repo: Path, output: Path, keep: Optional[Path], jobs: int) -> int:
    """Measure every case into `output`. With `keep`, leave each case's profile."""
    tools = Toolchain(repo)
    cases = read_cases(tools.tests / "e2e.cases")
    temporary = None if keep else tempfile.TemporaryDirectory(prefix="myprofiler-bench-")
    root = keep if keep else Path(temporary.name)
    root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda case: measure(tools, case, root), cases))
    if temporary:
        temporary.cleanup()

    counts = {name: count for name, count, _ in results if count is not None}
    failures = [(name, message) for name, count, message in results if count is None]
    table = {"format": 1, "total": sum(counts.values()), "cases": dict(sorted(counts.items()))}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
    print(f"{len(counts)} cases, {table['total']} instructions -> {output}")
    for name, message in failures:
        print(f"FAIL {name}: {message.splitlines()[-1] if message else 'no output'}")
    return 1 if failures else 0
