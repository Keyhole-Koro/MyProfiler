"""The last two recorded boots, each kept with the images it ran.

A profile holds addresses, and the next build moves them, so a run keeps its
own copy of the images. `build/profile/last` and `previous` are laid out like a
build directory, with the profile beside the images.
"""

import shutil
from pathlib import Path


def profile_of(build: Path, run: str) -> Path:
    return build / "profile" / run / "profile.json"


def start(build: Path) -> Path:
    """Where a new run writes its profile until it has finished."""
    output = profile_of(build, "next")
    shutil.rmtree(output.parent, ignore_errors=True)
    output.parent.mkdir(parents=True)
    return output


def finish(build: Path) -> Path:
    """Keep the images with the new run and make it the last one."""
    root = build / "profile"
    run = root / "next"
    for path in [*build.glob("*_linked.mbin*"), *(build / "user").glob("*.mbin*")]:
        target = run / path.relative_to(build)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.rmtree(root / "previous", ignore_errors=True)
    if (root / "last").exists():
        (root / "last").rename(root / "previous")
    run.rename(root / "last")
    return profile_of(build, "last")


def image_directory(profile: Path, build: Path) -> Path:
    """The images that go with a profile: its own if it kept them."""
    return profile.parent if any(profile.parent.glob("*_linked.mbin")) else build
