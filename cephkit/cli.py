# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""The `cephkit` command: one subcommand group per module, like `ceph orch`."""

import argparse
import os
import subprocess

from cephkit import osd


def version() -> str:
    """The release version, which only the release build writes into
    cephkit/_version.py; in a checkout, `git describe` if git is there."""
    try:
        from cephkit._version import VERSION
    except ImportError:
        pass
    else:
        return VERSION
    checkout = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Only cephkit's own checkout: sources copied into another repository
    # would otherwise report that repository's version.
    if not os.path.exists(os.path.join(checkout, ".git")):
        return "checkout"
    try:
        described = subprocess.run(
            ["git", "-C", checkout, "describe", "--tags", "--always", "--dirty"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        described = ""
    return f"checkout {described}".strip()


class VersionAction(argparse.Action):
    """--version, worked out only when asked for: a checkout runs git for it."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs: object) -> None:
        super().__init__(
            option_strings, dest, nargs=0, help="print cephkit's version and exit"
        )

    def __call__(self, parser, namespace, values, option_string=None) -> None:  # type: ignore[no-untyped-def]
        print(f"cephkit {version()}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cephkit",
        description="A command-line toolkit for operating Ceph clusters.",
    )
    parser.add_argument("--version", action=VersionAction)
    groups = parser.add_subparsers(
        title="command groups", dest="group", metavar="GROUP", required=True
    )
    osd.add_commands(groups)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.run(args)
