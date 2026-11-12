# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""cephadm's SSH key is held only in cephkit's own agent, and only for the run.

Uses the real ssh-agent, ssh-add and ssh-keygen, and a stand-in `ceph` that
serves a throwaway key as `config-key get mgr/cephadm/ssh_identity_key`.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from cephkit.system import cephadm_key_agent


def processes_naming(text: str) -> list[str]:
    """Processes whose command line contains `text`, read from /proc. An agent
    whose socket file was deleted is unreachable but still holds the key."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue  # it ended while we looked
        if text in cmdline:
            found.append(f"{pid}: {cmdline}")
    return found


def keys_in(socket: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh-add", "-l"],
        env={**os.environ, "SSH_AUTH_SOCK": socket},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


class CephadmKeyTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        bin_dir = pathlib.Path(scratch.name)
        key = bin_dir / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
        self.fingerprint = subprocess.run(
            ["ssh-keygen", "-lf", f"{key}.pub"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[1]
        ceph = bin_dir / "ceph"
        ceph.write_text(
            "#!/bin/bash\n"
            '[ "$*" = "config-key get mgr/cephadm/ssh_identity_key" ] || exit 22\n'
            f'cat "{key}"\n'
        )
        ceph.chmod(0o755)
        path = f"{bin_dir}:{os.environ['PATH']}"
        patcher = mock.patch.dict(os.environ, {"PATH": path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_key_lives_in_the_agent_only_during_the_run(self):
        for ending in ("normally", "with an error"):
            with self.subTest(ending=ending):
                try:
                    with cephadm_key_agent() as socket:
                        held = keys_in(socket)
                        self.assertEqual(held.returncode, 0, held.stderr)
                        self.assertIn(self.fingerprint, held.stdout)
                        # The private directory holds the socket and the key's
                        # public half, nothing else.
                        directory = os.path.dirname(socket)
                        self.assertEqual(
                            sorted(os.listdir(directory)), ["agent", "agent.pub"]
                        )
                        with open(os.path.join(directory, "agent.pub")) as public:
                            text = public.read()
                        self.assertTrue(text.startswith("ssh-ed25519 "), text)
                        self.assertNotIn("PRIVATE", text)
                        if ending == "with an error":
                            raise RuntimeError("a step failed")
                except RuntimeError:
                    pass
                self.assertEqual(processes_naming(socket), [])
                self.assertFalse(os.path.exists(os.path.dirname(socket)))
                self.assertNotEqual(keys_in(socket).returncode, 0)


if __name__ == "__main__":
    unittest.main()
