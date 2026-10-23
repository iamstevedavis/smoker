from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "smokestack" / "static"


def test_every_static_file_ships_in_the_package():
    """A file the dashboard serves must also be in the built wheel."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["smokestack"]
    files = [
        path.relative_to(STATIC.parent).as_posix()
        for path in STATIC.rglob("*")
        if path.is_file()
    ]

    missing = [
        name
        for name in files
        if not any(PurePosixPath(name).match(pattern) for pattern in patterns)
    ]

    assert not missing, f"not in package-data: {', '.join(sorted(missing))}"
