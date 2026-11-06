"""A profile from `myemu --profile`, tied to the images that produced it."""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .images import Image, ImageSet

KERNEL_SPACE = 0


def find_images(build_dir: Path) -> tuple[ImageSet, list[Image]]:
    """The kernel-side images and the user programs under a build directory."""
    system = [Image(path) for path in sorted(build_dir.glob("*_linked.mbin"))]
    users = [Image(path) for path in sorted((build_dir / "user").glob("*.mbin"))]
    return ImageSet(system, "kernel"), users


@dataclass(frozen=True)
class Function:
    program: str
    name: str
    self_count: int
    inclusive: int
    calls: int


class Profile:
    def __init__(self, path: Path, system, users: list[Image]):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))
        if self.data.get("format") != 2:
            raise ValueError(f"{path}: expected profile format 2")
        self.total = self.data["total_instructions"]
        self.programs = {KERNEL_SPACE: system}
        self.unknown_spaces = []
        for space in self.data["spaces"]:
            key = space["space"]
            if key == KERNEL_SPACE:
                continue
            sample = bytes.fromhex(space["text_sample"])
            image = next((i for i in users if i.matches(sample)), None)
            if image is None:
                self.unknown_spaces.append(key)
                continue
            self.programs[key] = image

    def program_name(self, space: int) -> str:
        program = self.programs.get(space)
        return program.name if program else f"space 0x{space:X}"

    def _program(self, space: int):
        return self.programs.get(space)

    def function_at(self, space: int, pc: int) -> Optional[str]:
        program = self._program(space)
        return program.function(pc) if program else None

    def label(self, space: int, pc: int) -> str:
        return self.function_at(space, pc) or f"0x{pc:08X}"

    def position_at(self, space: int, pc: int):
        program = self._program(space)
        return program.position(pc) if program else None

    def functions(self) -> list[Function]:
        rows = []
        for row in self.data["functions"]:
            rows.append(Function(self.program_name(row["space"]),
                                 self.label(row["space"], row["entry"]),
                                 row["self"], row["inclusive"], row["calls"]))
        return rows

    def by_function(self) -> dict[tuple[str, str], int]:
        """Instructions per (program, function), from the PCs themselves.

        Every retired instruction has a PC, so this adds up to the total even
        for code the call graph never saw entered.
        """
        totals: dict[tuple[str, str], int] = defaultdict(int)
        for row in self.data["pc_hits"]:
            key = (self.program_name(row["space"]), self.label(row["space"], row["pc"]))
            totals[key] += row["hits"]
        return dict(totals)

    def by_line(self) -> dict[tuple[str, int], int]:
        totals: dict[tuple[str, int], int] = defaultdict(int)
        for row in self.data["pc_hits"]:
            position = self.position_at(row["space"], row["pc"])
            if position:
                totals[(position.file, position.line)] += row["hits"]
        return dict(totals)

    def by_cost(self) -> dict[str, int]:
        """Instructions per cost kind. "" is ordinary code, None no line info."""
        totals: dict = defaultdict(int)
        for row in self.data["pc_hits"]:
            position = self.position_at(row["space"], row["pc"])
            totals[position.cost if position else None] += row["hits"]
        return dict(totals)

    def by_program(self) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for space in self.data["spaces"]:
            if space["instructions"]:
                totals[self.program_name(space["space"])] += space["instructions"]
        return dict(totals)

    def line_costs(self, file_suffix: str) -> tuple[Optional[str], dict[int, int]]:
        """Instructions per line of the one file ending in `file_suffix`."""
        counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for (file, line), hits in self.by_line().items():
            if file.endswith(file_suffix):
                counts[file][line] += hits
        if not counts:
            return None, {}
        file = max(counts, key=lambda name: sum(counts[name].values()))
        return file, dict(counts[file])
