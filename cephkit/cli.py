# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""The `cephkit` command: one subcommand group per module, like `ceph orch`."""

import argparse

from cephkit import osd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cephkit",
        description="A command-line toolkit for operating Ceph clusters.",
    )
    groups = parser.add_subparsers(
        title="command groups", dest="group", metavar="GROUP", required=True
    )
    osd.add_commands(groups)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.run(args)
