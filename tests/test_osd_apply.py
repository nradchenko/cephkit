# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""`cephkit osd add --apply`: each step is checked before the next one starts."""

import unittest

from cephkit import osd
from cephkit.system import CommandError

GiB = 2**30
FSID = "0f6c1a52-1b2c-4d3e-8f90-123456789abc"

HDD = osd.Hdd("/dev/sdd", "SN0003", "ATA_EXAMPLE_HDD16T_SN0003", 16 * 10**12)
STEP = osd.NewOsd(HDD, "ssd-1", "db-SN0003", create_lv=True, why="")
FACTS = osd.HostFacts(
    name="host1",
    hdds=[HDD],
    vgs=[osd.DbVg("ssd-1", 1, 1000 * GiB, 4 * 2**20, "md1")],
    lvs=[],
)
TAGS = {"ceph.type": "db", "ceph.osd_id": "12", "ceph.osd_fsid": FSID}
BOOTED = {
    "id": 12,
    "device_ids": "sdd=ATA_EXAMPLE_HDD16T_SN0003",
    "bluefs_dedicated_db": "1",
    "bluefs_db_devices": "md1",
}


class Script:
    """A host and cluster that answer one way. The clock moves only when
    cephkit sleeps or a step takes time."""

    def __init__(
        self,
        serial="SN0003",
        tags=TAGS,
        boots_after=150,
        metadata=BOOTED,
        info_errors=0,
        map_uuid=FSID,
    ):
        self._map_uuid = map_uuid  # whose uuid the OSD map shows under the id
        self._serial = serial
        self._tags = tags
        self._boots_after = boots_after  # seconds after the OSD is added
        self._metadata = metadata
        self._info_errors = info_errors  # osd info fails this many times first
        self.clock = 0.0
        self.added_at = None
        self.calls = []

    def serial(self, path):
        self.calls.append("serial")
        return self._serial

    def create_lv(self, vg, lv, size):
        self.calls.append("lvcreate")

    def add_osd(self, path, vg, lv, tick):
        self.calls.append("add_osd")
        self.clock += 70  # ceph-volume at work
        self.added_at = self.clock

    def lv_tags(self, vg, lv):
        self.calls.append("tags")
        return dict(self._tags)

    def osd_info(self, osd_id):
        self.calls.append("info")
        if self._info_errors:
            self._info_errors -= 1
            raise CommandError("mon election")
        up = self._boots_after is not None and (
            self.clock - self.added_at >= self._boots_after
        )
        return {"osd": osd_id, "uuid": self._map_uuid, "up": int(up)}

    def osd_metadata(self, osd_id):
        self.calls.append("metadata")
        return dict(self._metadata)

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds


def apply(script):
    return osd.apply_step(STEP, 128 * GiB, FACTS, script, say=lambda text: None)


class ApplyTest(unittest.TestCase):
    def test_a_good_osd_is_created_and_checked_in_order(self):
        # One failed `osd info` (a mon election) is waited out, not a stop.
        script = Script(info_errors=1)
        self.assertEqual(apply(script), 12)
        self.assertEqual(script.calls[:4], ["serial", "lvcreate", "add_osd", "tags"])
        self.assertEqual(script.calls[-2:], ["info", "metadata"])

    def test_apply_stops_at_the_first_mismatch(self):
        cases = [
            # sdX names moved: nothing may be touched.
            ("moved", Script(serial="SN0009"), ["serial"]),
            # cephadm 20.2 exits 0 when ceph-volume fails: no tag, no waiting.
            ("untagged", Script(tags={}), ["serial", "lvcreate", "add_osd", "tags"]),
            ("never boots", Script(boots_after=None), None),
            # Up under our id, but not the OSD ceph-volume just made: its old
            # metadata must never be read as the new OSD's.
            ("another osd under the id", Script(map_uuid="old-uuid"), None),
            (
                "no dedicated db",
                Script(metadata=dict(BOOTED, bluefs_dedicated_db="0")),
                None,
            ),
            (
                "db on another array",
                Script(metadata=dict(BOOTED, bluefs_db_devices="md2")),
                None,
            ),
            (
                "another hdd",
                Script(metadata=dict(BOOTED, device_ids="sdz=ATA_X_SN9")),
                None,
            ),
        ]
        for name, script, calls in cases:
            with self.subTest(name), self.assertRaises(osd.ApplyStop):
                try:
                    apply(script)
                finally:
                    if calls is not None:
                        self.assertEqual(script.calls, calls)
                    if name == "never boots":
                        waited = script.clock - script.added_at
                        self.assertGreaterEqual(waited, osd.BOOT_TIMEOUT)
                        self.assertLess(waited, osd.BOOT_TIMEOUT + osd.POLL_EVERY + 1)


if __name__ == "__main__":
    unittest.main()
