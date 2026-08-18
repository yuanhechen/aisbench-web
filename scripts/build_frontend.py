"""Build the React application and copy it into the Python package.

Run this before `python -m build`. The target directory is removed and recreated, so the
path is verified to be the expected one inside this repository first.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPOSITORY_ROOT / "frontend"
BUILD_OUTPUT = FRONTEND_DIR / "dist"
PACKAGE_STATIC = REPOSITORY_ROOT / "src" / "aisbench_web" / "static"


def _run(command: list[str]) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=FRONTEND_DIR, check=True)


def _validated_static_dir() -> Path:
    """Refuse to delete anything that is not exactly the packaged static directory."""
    target = PACKAGE_STATIC.resolve()
    expected = (REPOSITORY_ROOT / "src" / "aisbench_web" / "static").resolve()
    if target != expected or REPOSITORY_ROOT.resolve() not in target.parents:
        raise SystemExit(f"Refusing to remove {target}: it is not the packaged static directory")
    return target


def main() -> int:
    if not FRONTEND_DIR.is_dir():
        raise SystemExit(f"No frontend directory at {FRONTEND_DIR}")

    _run(["npm", "ci"])
    _run(["npm", "run", "build"])
    if not (BUILD_OUTPUT / "index.html").is_file():
        raise SystemExit(f"The frontend build produced no index.html in {BUILD_OUTPUT}")

    target = _validated_static_dir()
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(BUILD_OUTPUT, target)
    copied = sum(1 for path in target.rglob("*") if path.is_file())
    print(f"Copied {copied} file(s) into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
