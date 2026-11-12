# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""How cephkit talks to the outside world: `ceph` on this node, SSH to the others."""

import json
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class CommandError(Exception):
    """A command exited non-zero. The message carries the command and its stderr."""


def run(argv: list[str]) -> str:
    return run_with_heartbeat(argv, tick=None)


def run_with_heartbeat(
    argv: list[str],
    tick: Callable[[float], None] | None,
    every: float = 30,
) -> str:
    """Run a command and return its stdout; call `tick(seconds so far)` every
    `every` seconds while it runs, so a long command shows it is alive.

    stdin is /dev/null: a prompt on the far side of ssh (lvcreate's "Wipe it?
    [y/n]") reads end of input and fails, instead of waiting on the terminal.
    """
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    try:
        while True:
            try:
                # communicate() may be called again after a timeout; nothing is lost.
                stdout, stderr = process.communicate(timeout=every if tick else None)
                break
            except subprocess.TimeoutExpired:
                if tick:
                    tick(time.monotonic() - started)
    except BaseException:
        # Ctrl-C, or `tick` failing on a closed pipe: don't leave the child
        # running unseen. (For `ceph orch`, the mgr's own work goes on anyway.)
        process.kill()
        process.wait()
        raise
    if process.returncode != 0:
        raise CommandError(
            f"`{shlex.join(argv)}` exited {process.returncode}: {stderr.strip()}"
        )
    return stdout


def ceph_json(*args: str) -> Any:
    """Run `ceph ARGS --format json`. Orchestrator commands produce json.dumps()
    of their objects; mon commands produce a Formatter dump. Both parse as JSON."""
    return json.loads(run(["ceph", *args, "--format", "json"]))


def ceph_text(*args: str) -> str:
    """Run a `ceph` command that prints plain text whatever --format says, such as
    `cephadm get-user` and `config get`."""
    return run(["ceph", *args]).strip()


@dataclass(frozen=True)
class Host:
    """A storage node, reached the way cephadm reaches it: its address from
    `ceph orch host ls`, the user from `ceph cephadm get-user`, and sudo when
    that user is not root. The operator's own SSH key and config authenticate."""

    name: str
    addr: str
    user: str

    def run(self, argv: list[str], *, root: bool = False) -> str:
        command = shlex.join(argv)
        if root and self.user != "root":
            command = "sudo -n " + command
        # BatchMode: fail at once instead of waiting for a password nobody types.
        return run(["ssh", "-o", "BatchMode=yes", f"{self.user}@{self.addr}", command])
