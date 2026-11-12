# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""How cephkit talks to the outside world: `ceph` on this node, SSH to the others."""

import contextlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
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


CEPHADM_KEY = "mgr/cephadm/ssh_identity_key"


def agent_public_key(socket: str) -> str:
    """Where cephadm_key_agent puts the public half of the key it holds."""
    return socket + ".pub"


def ssh_add(args: list[str], socket: str, key: str = "") -> subprocess.CompletedProcess:
    """Run ssh-add against one agent. It gets no terminal and no askpass, and a
    time limit, so a key that wants a passphrase fails instead of waiting."""
    try:
        return subprocess.run(
            ["ssh-add", *args],
            input=key,
            env={**os.environ, "SSH_AUTH_SOCK": socket, "SSH_ASKPASS_REQUIRE": "never"},
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CommandError(f"`ssh-add {shlex.join(args)}`: {error}") from None


@contextlib.contextmanager
def cephadm_key_agent() -> Iterator[str]:
    """Hold cephadm's SSH key for one run, in a private ssh-agent that this
    process starts and always stops. Yields the agent's socket.

    cephadm keeps its key in the cluster, where the admin keyring can read it.
    Here it goes from `ceph config-key get` straight into `ssh-add -`, which
    reads a key from stdin, so it is never written to disk; it is gone when
    the agent stops. Only its public half is written, next to the socket, so
    that ssh can be told to offer this key and no other."""
    directory = tempfile.mkdtemp(prefix="cephkit-")  # created 0700
    socket = os.path.join(directory, "agent")
    agent = None
    try:
        if len(socket.encode()) > 100:  # a Unix socket path holds 108 bytes
            raise CommandError(
                f"the agent's socket path {socket} is too long for a Unix socket: "
                "point TMPDIR at a shorter directory"
            )
        try:
            agent = subprocess.Popen(
                ["ssh-agent", "-D", "-a", socket],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise CommandError(f"can't start ssh-agent: {error}") from None
        # Ready when ssh-add reaches it: exit 1 means "no identities yet",
        # exit 2 "can't connect".
        deadline = time.monotonic() + 10
        while ssh_add(["-l"], socket).returncode not in (0, 1):
            if agent.poll() is not None or time.monotonic() > deadline:
                raise CommandError("ssh-agent did not start")
            time.sleep(0.05)
        added = ssh_add(["-"], socket, run(["ceph", "config-key", "get", CEPHADM_KEY]))
        if added.returncode != 0:
            raise CommandError(
                f"`ssh-add -` exited {added.returncode}: {added.stderr.strip()}"
            )
        listed = ssh_add(["-L"], socket)
        if listed.returncode != 0:
            raise CommandError(f"`ssh-add -L` exited {listed.returncode}")
        with open(agent_public_key(socket), "w") as public:
            public.write(listed.stdout)
        yield socket
    finally:
        if agent:
            agent.kill()
            agent.wait()
        for path in (socket, agent_public_key(socket)):
            if os.path.exists(path):
                os.remove(path)
        os.rmdir(directory)


@dataclass(frozen=True)
class Host:
    """A storage node, reached the way cephadm reaches it: its address from
    `ceph orch host ls`, the user from `ceph cephadm get-user`, sudo when that
    user is not root, and, unless the operator asks for their own SSH setup,
    cephadm's key from `cephadm_key_agent`."""

    name: str
    addr: str
    user: str
    agent: str | None = None  # cephadm_key_agent's socket

    def run(self, argv: list[str], *, root: bool = False) -> str:
        command = shlex.join(argv)
        if root and self.user != "root":
            command = "sudo -n " + command
        # BatchMode: fail at once instead of waiting for a password nobody types.
        options = ["-o", "BatchMode=yes"]
        if self.agent:
            # Log in exactly as cephadm does. -F /dev/null: no ssh_config of the
            # operator's or the system's changes the user, address or route.
            # IdentitiesOnly with the public key: ssh offers cephadm's key from
            # this agent and no other key. The agent is never forwarded. A
            # host's key is taken on first contact into the operator's
            # known_hosts, and a changed one is refused.
            options = [
                "-F",
                "/dev/null",
                *options,
                "-o",
                f"IdentityAgent={self.agent}",
                "-o",
                f"IdentityFile={agent_public_key(self.agent)}",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ForwardAgent=no",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
        return run(["ssh", *options, f"{self.user}@{self.addr}", command])
