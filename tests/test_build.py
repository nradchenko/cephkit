# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""The single-file release built by tools/build.py."""

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).parent.parent


def build(version: str, output: pathlib.Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "build.py"),
            "--version",
            version,
            "--output",
            str(output),
        ],
        check=True,
    )


class BuildTest(unittest.TestCase):
    def test_the_release_file_is_reproducible_and_keeps_version_and_status(self):
        with tempfile.TemporaryDirectory() as scratch:
            scratch = pathlib.Path(scratch)
            first, second = scratch / "a" / "cephkit", scratch / "b" / "cephkit"
            build("9.9.9-test", first)
            build("9.9.9-test", second)
            # Same commit, same version: the same bytes.
            self.assertEqual(first.read_bytes(), second.read_bytes())
            # And on another machine or day: nothing in the archive depends on
            # when or where it was built, or on the zlib version.
            with zipfile.ZipFile(first) as archive:
                for info in archive.infolist():
                    with self.subTest(entry=info.filename):
                        self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                        self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                        mode = info.external_attr >> 16
                        self.assertTrue(stat.S_ISREG(mode))
                        self.assertEqual(stat.S_IMODE(mode), 0o644)
                        self.assertEqual(info.create_system, 3)  # Unix

            shown = subprocess.run(
                [str(first), "--version"], capture_output=True, text=True, check=True
            )
            self.assertEqual(shown.stdout.strip(), "cephkit 9.9.9-test")

            # A failure that main() returns, not one argparse exits with:
            # `ceph` fails, run_add returns 1, and the archive must exit 1.
            ceph = scratch / "ceph"
            ceph.write_text("#!/bin/bash\necho 'stand-in ceph fails' >&2\nexit 22\n")
            ceph.chmod(0o755)
            failed = subprocess.run(
                [str(first), "osd", "add", "--all-hosts"],
                env={**os.environ, "PATH": f"{scratch}:{os.environ['PATH']}"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(failed.returncode, 1, failed.stderr)
            self.assertIn("stand-in ceph fails", failed.stderr)


if __name__ == "__main__":
    unittest.main()
