# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""Build the single-file cephkit: a zip archive that Python runs directly.

    python3 tools/build.py --version 0.1.0 --output dist/cephkit

The same commit and version always give the same bytes, so anyone can rebuild
a release and compare its checksum. That rules out the standard zipapp module,
which stamps each file with its modification time in local time and whose
generated __main__.py drops the exit status. Here every entry gets a fixed date
and mode, entries are stored uncompressed (compressed bytes would depend on the
zlib version), and the archive's own __main__.py passes the status on. The
version exists only in the archive, as cephkit/_version.py.
"""

import argparse
import pathlib
import re
import stat
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHEBANG = b"#!/usr/bin/env python3\n"
FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # the earliest a zip entry can carry
HEADER = "# SPDX-License-Identifier: MIT\n# Copyright (c) 2026 Nikita Radchenko\n"

MAIN = (
    HEADER
    + '''"""Runs the cephkit release file."""

import sys

from cephkit.cli import main

sys.exit(main())
'''
)


def entries(version: str) -> list[tuple[str, bytes]]:
    sources = sorted((ROOT / "cephkit").glob("*.py"))
    files = [(f"cephkit/{path.name}", path.read_bytes()) for path in sources]
    files.append(("cephkit/_version.py", f'{HEADER}VERSION = "{version}"\n'.encode()))
    files.append(("__main__.py", MAIN.encode()))
    return sorted(files)


def warn_if_not_committed() -> None:
    """The build packs cephkit/ as it is on disk. Edited or untracked files
    make a file that matches no commit: say so, since a checksum won't."""
    try:
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", "cephkit"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return  # not a git checkout, or no git: nothing to compare with
    if status.strip():
        print(
            "build.py: cephkit/ has uncommitted or untracked files; "
            "this build matches no commit:\n" + status,
            file=sys.stderr,
        )


def build(version: str, output: pathlib.Path) -> None:
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise SystemExit(f"build.py: {version!r} is not a version")
    warn_if_not_committed()
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as target:
        target.write(SHEBANG)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
            for name, data in entries(version):
                info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                # "Made on Unix" whatever the build runs on: zipfile would
                # write 0 on Windows, and so would differ there.
                info.create_system = 3
                archive.writestr(info, data)
    output.chmod(0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--version", required=True, help="the release version, like 0.1.0"
    )
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    build(args.version, args.output)
