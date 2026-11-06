"""A linked image's symbols and source positions.

`<image>.mbin.map` names symbols and `<image>.mbin.lines` holds what the
compiler said about each address. docs/formats.md describes both.
"""

import bisect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TEXT_RANGE = re.compile(r"# text: 0x([0-9a-fA-F]+) \.\. 0x([0-9a-fA-F]+)")
MANGLED = re.compile(r"__mlg_[sf]_(\d+)_")


def short_name(name: str) -> str:
    """Read a function name the way the source writes it.

    `Text__len` is the method `Text.len`. An instantiated generic carries its
    type arguments in the name, which `<>` stands for:
    `__mlg_s_4_Text_c2_12__len` is `Text<>.len`.
    """
    match = MANGLED.match(name)
    if match:
        start = match.end()
        base = name[start:start + int(match.group(1))]
        method = name.rsplit("__", 1)[1] if "__" in name[start:] else ""
        return f"{base}<>.{method}" if method else f"{base}<>"
    if "__" in name.strip("_"):
        return name.replace("__", ".")
    return name


@dataclass(frozen=True)
class Position:
    file: str
    line: int
    cost: str  # "" outside a cost region


class Image:
    """One linked program. A missing `.lines` leaves functions from the map."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name.split(".")[0].removesuffix("_linked")
        self.text_start = 0
        self.text_end = 0
        self._starts: list[int] = []
        self._names: list[str] = []
        self._row_starts: list[int] = []
        self._rows: list[Position] = []
        functions = self._read_map(self._sibling(".map"))
        functions.update(self._read_lines(self._sibling(".lines")))
        for address in sorted(functions):
            self._starts.append(address)
            self._names.append(functions[address])

    def _sibling(self, suffix: str) -> Path:
        return self.path.with_name(self.path.name + suffix)

    def _read_map(self, path: Path) -> dict[int, str]:
        functions: dict[int, str] = {}
        if not path.exists():
            return functions
        for line in path.read_text(encoding="utf-8").splitlines():
            text_range = TEXT_RANGE.match(line)
            if text_range:
                self.text_start = int(text_range.group(1), 16)
                self.text_end = int(text_range.group(2), 16)
                continue
            if line.startswith("#"):
                continue
            address, _, name = line.partition(" ")
            value = int(address, 16)
            # A tagged name is a referenced block label, which is inside a
            # function; `.lines` names the private functions.
            if self.contains(value) and "-[" not in name:
                functions[value] = short_name(name)
        return functions

    def _read_lines(self, path: Path) -> dict[int, str]:
        functions: dict[int, str] = {}
        if not path.exists():
            return functions
        files: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "file":
                index, _, name = rest.partition(" ")
                files[index] = name
            elif kind == "func":
                address, _, rest = rest.partition(" ")
                _, _, name = rest.partition(" ")
                functions[int(address, 16)] = short_name(name)
            elif kind == "line":
                address, file, number, cost = rest.split(" ")
                self._row_starts.append(int(address, 16))
                self._rows.append(Position(files[file], int(number), "" if cost == "-" else cost))
        return functions

    def contains(self, pc: int) -> bool:
        return self.text_start <= pc < self.text_end

    def function(self, pc: int) -> Optional[str]:
        """The function holding `pc`, or None outside this image's text."""
        if not self.contains(pc):
            return None
        index = bisect.bisect_right(self._starts, pc) - 1
        return self._names[index] if index >= 0 else None

    def position(self, pc: int) -> Optional[Position]:
        if not self.contains(pc):
            return None
        index = bisect.bisect_right(self._row_starts, pc) - 1
        return self._rows[index] if index >= 0 else None

    def matches(self, sample: bytes) -> bool:
        """Whether a text sample from a running process is this program."""
        if not sample or not self.path.exists():
            return False
        # The page holds more than the text; only the text is in the file as is.
        length = min(len(sample), self.text_end - self.text_start)
        data = self.path.read_bytes()
        at = data.find(sample[:min(length, 64)])
        return at >= 0 and data[at:at + length] == sample[:length]


class ImageSet:
    """Several images sharing an address space: firmware and kernel."""

    def __init__(self, images: list[Image], name: str):
        self.images = images
        self.name = name

    def function(self, pc: int) -> Optional[str]:
        return next((n for n in (i.function(pc) for i in self.images) if n), None)

    def position(self, pc: int) -> Optional[Position]:
        return next((p for p in (i.position(pc) for i in self.images) if p), None)
