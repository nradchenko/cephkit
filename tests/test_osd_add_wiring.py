# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""`cephkit osd add` end to end, with stand-ins for `ceph` and `ssh`.

The stand-ins print fixtures for the commands cephkit is expected to run, fail
on any other, and log each call with what their stdin is. For --apply they
change state like a cluster: adding the OSD on sdd tags its DB volume and
brings osd.12 up; adding the one on sde exits 0 and does nothing, as cephadm
20.2 does when ceph-volume fails.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic-18.2"
FSID = "0f6c1a52-1b2c-4d3e-8f90-123456789abc"

FAKE_CEPH = """#!/bin/bash
printf 'ceph %s\\n' "$*" >> "$CALLS"
case "$*" in
  "cephadm get-user") cat "$FIXTURES/cephadm-get-user.txt" ;;
  "config-key get mgr/cephadm/ssh_identity_key") cat "$KEY" ;;
  "orch host ls --format json") cat "$FIXTURES/orch-host-ls.json" ;;
  "orch device ls --format json") cat "$FIXTURES/orch-device-ls.json" ;;
  "config get osd bluestore_rocksdb_options") cat "$FIXTURES/rocksdb-options.txt" ;;
  "orch daemon add osd host1:data_devices=/dev/sdd,db_devices=/dev/ssd-1/db-SN0003")
    touch "$STATE/sdd-added" ;;
  "orch daemon add osd host1:data_devices=/dev/sde,db_devices=/dev/ssd-2/db-SN0004") ;;
  "osd info osd.12 --format json")
    echo '{"osd": 12, "uuid": "@FSID@", "up": 1, "in": 1}' ;;
  "osd metadata 12 --format json")
    echo '{"id": 12, "bluefs_dedicated_db": "1", "bluefs_db_devices": "md1",'\\
         ' "device_ids": "sdd=ATA_EXAMPLE_HDD16T_SN0003"}' ;;
  *) echo "unexpected: ceph $*" >&2; exit 22 ;;
esac
""".replace("@FSID@", FSID)

FAKE_SSH = """#!/bin/bash
remote="${*: -1}"
printf 'ssh %s stdin=%s\\n' "$*" "$(readlink /proc/self/fd/0)" >> "$CALLS"
lv() { printf '{"report": [{"lv": [{"lv_name": "%s", "vg_name": "%s",'\\
' "lv_size": "137438953472", "lv_tags": "%s"}]}]}\\n' "$1" "$2" "$3"; }
case "$remote" in
  "lsblk "*" /dev/sdd") echo '{"blockdevices": [{"path": "/dev/sdd", "serial": "SN0003"}]}' ;;
  "lsblk "*" /dev/sde") echo '{"blockdevices": [{"path": "/dev/sde", "serial": "SN0004"}]}' ;;
  "lsblk "*) cat "$FIXTURES/host1-lsblk.json" ;;
  "sudo -n pvs "*) cat "$FIXTURES/host1-pvs.json" ;;
  "sudo -n vgs "*) cat "$FIXTURES/host1-vgs.json" ;;
  "sudo -n lvcreate "*) ;;
  "sudo -n lvs "*" ssd-1/db-SN0003")
    tags=""
    [ -e "$STATE/sdd-added" ] && tags="ceph.osd_fsid=@FSID@,ceph.osd_id=12,ceph.type=db"
    lv db-SN0003 ssd-1 "$tags" ;;
  "sudo -n lvs "*" ssd-2/db-SN0004") lv db-SN0004 ssd-2 "" ;;
  "sudo -n lvs "*) cat "$FIXTURES/host1-lvs.json" ;;
  *) echo "unexpected: ssh $*" >&2; exit 255 ;;
esac
""".replace("@FSID@", FSID)


def run_cephkit(*args, env_extra=None):
    """Run bin/cephkit with the stand-ins first on PATH. Returns the result and
    the logged calls."""
    with tempfile.TemporaryDirectory() as scratch:
        bin_dir = pathlib.Path(scratch)
        calls = bin_dir / "calls.log"
        calls.touch()
        for name, text in (("ceph", FAKE_CEPH), ("ssh", FAKE_SSH)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        # A throwaway key for the stand-in to serve as cephadm's.
        key = bin_dir / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            FIXTURES=str(FIXTURES),
            CALLS=str(calls),
            STATE=str(bin_dir),
            KEY=str(key),
            **(env_extra or {}),
        )
        # An open pipe on stdin: only cephkit's own /dev/null closes it.
        result = subprocess.run(
            [str(ROOT / "bin" / "cephkit"), "osd", "add", *args],
            stdin=subprocess.PIPE,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return result, calls.read_text().splitlines()


class WiringTest(unittest.TestCase):
    def test_all_hosts_plan_through_the_real_commands(self):
        result, log = run_cephkit("--all-hosts")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-> ssd-1/db-SN0003", result.stdout)
        self.assertIn("-> ssd-2/db-SN0004", result.stdout)
        self.assertIn("skipped host host2: maintenance", result.stdout)
        self.assertIn("skipped host host3: offline", result.stdout)

        ssh_calls = [line for line in log if line.startswith("ssh ")]
        self.assertEqual(len(ssh_calls), 4, log)
        for line in ssh_calls:
            self.assertIn("ceph@192.0.2.11 ", line)
            self.assertTrue(line.endswith("stdin=/dev/null"), line)
            if " lsblk " not in line:
                self.assertIn(" sudo -n ", line)

    def test_ssh_uses_cephadms_key_unless_asked_not_to(self):
        # In cephadm mode no ssh_config takes part and only cephadm's key is
        # offered; in own-key mode none of this is forced.
        cephadm_mode = [
            "ssh -F /dev/null ",
            "-o IdentityAgent=",
            "-o IdentityFile=",
            "-o IdentitiesOnly=yes",
            "-o ForwardAgent=no",
            "-o StrictHostKeyChecking=accept-new",
        ]
        for args, env, uses_cephadm_key in (
            (["--all-hosts"], {}, True),
            (["--all-hosts", "--ssh-own-key"], {}, False),
            (["--all-hosts"], {"CEPHKIT_SSH": "own"}, False),
        ):
            with self.subTest(args=args, env=env):
                result, log = run_cephkit(*args, env_extra=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                read_key = "ceph config-key get mgr/cephadm/ssh_identity_key" in log
                self.assertEqual(read_key, uses_cephadm_key)
                ssh_calls = [line for line in log if line.startswith("ssh ")]
                self.assertEqual(len(ssh_calls), 4, log)
                for line in ssh_calls:
                    for option in cephadm_mode:
                        self.assertEqual(option in line, uses_cephadm_key, line)

    def test_an_unknown_ssh_mode_is_refused_before_any_login(self):
        result, log = run_cephkit("--all-hosts", env_extra={"CEPHKIT_SSH": "OWN"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("CEPHKIT_SSH", result.stderr)
        self.assertFalse([line for line in log if line.startswith("ssh ")], log)

    def test_apply_runs_the_real_commands_and_stops_on_a_silent_failure(self):
        result, log = run_cephkit("host1", "--all-available-devices", "--apply")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("osd.12 up with its DB on md1", result.stdout)
        self.assertIn("ceph-volume did not take ssd-2/db-SN0004", result.stdout)
        self.assertIn("ssd-2/db-SN0004 exists, untagged", result.stdout)
        self.assertIn("1 OSD(s) created before the stop", result.stdout)
        # Exact commands for the steps that change things.
        lvcreate = (
            "sudo -n lvcreate --wipesignatures y --zero y"
            " -n db-SN0003 -L 137438953472b ssd-1"
        )
        self.assertTrue(
            any(line.endswith(f"{lvcreate} stdin=/dev/null") for line in log)
        )
        self.assertIn(
            "ceph orch daemon add osd host1:data_devices=/dev/sdd,"
            "db_devices=/dev/ssd-1/db-SN0003",
            log,
        )


if __name__ == "__main__":
    unittest.main()
