# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""Command-line forms of `cephkit osd add` that would do the wrong thing."""

import argparse
import unittest

from cephkit import osd
from cephkit.cli import build_parser


class OsdAddArgsTest(unittest.TestCase):
    def test_only_the_four_modes_are_accepted(self) -> None:
        cases = [
            (["host1", "/dev/sdb"], True),
            (["host1", "--any"], True),
            (["host1", "--all-available-devices"], True),
            (["--all-hosts"], True),
            ([], False),
            (["host1"], False),
            (["host1", "/dev/sdb", "--any"], False),
            (["host1", "--all-hosts"], False),
        ]
        for argv, accepted in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(["osd", "add", *argv])
                self.assertEqual(osd.check_add_scope(args) is None, accepted)

    def test_sizes_are_binary_and_need_a_unit(self) -> None:
        self.assertEqual(osd.parse_size("128G"), 128 * 2**30)
        for text in ("128", "128GB", "1.5T", "0G"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                osd.parse_size(text)

    def test_a_vg_prefix_ceph_volume_would_claim_is_refused(self) -> None:
        for text in ("ceph", "Ceph-db"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                osd.parse_vg_prefix(text)


if __name__ == "__main__":
    unittest.main()
