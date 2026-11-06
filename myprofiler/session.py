"""Emulator arguments for profiling the whole system."""

from pathlib import Path
from typing import Optional

THREAD_SYMBOL = "g_current_thread_slot"


def thread_variable(build_dir: Path) -> Optional[int]:
    """The kernel word holding the current thread, from the kernel's map."""
    symbol_map = build_dir / "kernel_linked.mbin.map"
    if not symbol_map.exists():
        return None
    for line in symbol_map.read_text(encoding="utf-8").splitlines():
        address, _, name = line.partition(" ")
        if name == THREAD_SYMBOL or name.startswith(THREAD_SYMBOL + "-["):
            return int(address, 16)
    return None


def emulator_arguments(build_dir: Path, output: Path) -> list[str]:
    """`myemu` flags for a profile that follows threads."""
    arguments = ["--profile", str(output)]
    variable = thread_variable(build_dir)
    if variable is not None:
        arguments += ["--profile-thread-var", hex(variable)]
    return arguments
