"""MyLinker's object-format module, for the names MyLinker writes in maps.

MyLinker is a sibling repository, not a package, so its tools/objformat.py is
loaded by path from the MyComputer checkout this package sits in, the way
qa/tools/obj-viewer.py loads it.
"""

import importlib.util
from pathlib import Path

OBJFORMAT = Path(__file__).resolve().parents[3] / "toolchain" / "MyLinker" / "tools" / "objformat.py"

_spec = importlib.util.spec_from_file_location("objformat", OBJFORMAT)
objformat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(objformat)
