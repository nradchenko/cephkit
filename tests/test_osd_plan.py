# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""Planning `cephkit osd add` from what the cluster and the host report.

The fixture host has DB volume groups ssd-1 and ssd-2, one DB in use in each,
two available HDDs (SN0003, SN0004) and an unused db-SN0004 in ssd-2.
"""

import copy
import json
import pathlib
import unittest

from cephkit import osd

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "synthetic-18.2"
GiB = 2**30


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def facts(change=None):
    """host1's facts, after `change` edits the raw outputs in place."""
    raw = {
        "inventory": load("orch-device-ls.json")[0],
        "lsblk": load("host1-lsblk.json"),
        "pvs": load("host1-pvs.json"),
        "vgs": load("host1-vgs.json"),
        "lvs": load("host1-lvs.json"),
    }
    raw = copy.deepcopy(raw)
    if change:
        change(raw)
    return osd.host_facts("host1", vg_prefix="ssd", **raw)


def lv(raw, name):
    return next(row for row in raw["lvs"]["report"][0]["lv"] if row["lv_name"] == name)


def blockdevice(raw, path):
    return next(dev for dev in raw["lsblk"]["blockdevices"] if dev["path"] == path)


def outcome(plan, serial):
    """What the plan does with the HDD of this serial, in short."""
    step = next(step for step in plan.steps if step.hdd.serial == serial)
    if isinstance(step, osd.NewOsd):
        return f"{'create' if step.create_lv else 'reuse'} {step.vg}/{step.lv}"
    return "skipped: " + step.why


class PlanTest(unittest.TestCase):
    def test_db_goes_to_the_vg_with_fewest_dbs_and_an_unused_lv_is_reused(self):
        plan = osd.plan_host(facts())
        self.assertEqual(plan.db_size, 128 * GiB)
        # ssd-1 and ssd-2 both hold one DB in use: the lower number wins.
        self.assertEqual(outcome(plan, "SN0003"), "create ssd-1/db-SN0003")
        self.assertEqual(outcome(plan, "SN0004"), "reuse ssd-2/db-SN0004")

    def test_a_db_volume_that_is_taken_or_duplicated_is_never_used(self):
        def taken(raw):
            lv(raw, "db-SN0004")["lv_tags"] = "ceph.osd_id=7,ceph.type=db"

        def duplicated(raw):
            stale = dict(lv(raw, "db-SN0004"), vg_name="ssd-1")
            raw["lvs"]["report"][0]["lv"].append(stale)

        for change, expected in ((taken, "osd.7"), (duplicated, "ssd-2, ssd-1")):
            with self.subTest(change=change.__name__):
                self.assertIn(expected, outcome(osd.plan_host(facts(change)), "SN0004"))

    def test_the_host_is_refused_when_its_layout_is_unclear(self):
        def raid0(raw):
            blockdevice(raw, "/dev/md1")["type"] = "raid0"

        def mixed_sizes(raw):
            lv(raw, "db-SN0002")["lv_size"] = str(64 * GiB)

        def no_dbs_in_use(raw):
            for name in ("db-SN0001", "db-SN0002"):
                lv(raw, name)["lv_tags"] = ""

        for change in (raid0, mixed_sizes, no_dbs_in_use):
            with self.subTest(change=change.__name__), self.assertRaises(osd.PlanError):
                osd.plan_host(facts(change))

    def test_an_hdd_whose_identity_or_room_is_in_doubt_is_skipped(self):
        def moved(raw):
            blockdevice(raw, "/dev/sdd")["serial"] = "SN0009"

        def full(raw):
            for vg in raw["vgs"]["report"][0]["vg"]:
                vg["vg_free"] = str(100 * GiB)

        def twin(raw):
            # Two paths report one serial: only the first may become an OSD.
            blockdevice(raw, "/dev/sde")["serial"] = "SN0003"
            for device in raw["inventory"]["devices"]:
                if device["path"] == "/dev/sde":
                    device["device_id"] = "ATA_EXAMPLE_HDD16T_SN0003"

        cases = ((moved, "/dev/sdd"), (full, "/dev/sdd"), (twin, "/dev/sde"))
        for change, path in cases:
            with self.subTest(change=change.__name__):
                plan = osd.plan_host(facts(change))
                step = next(step for step in plan.steps if step.hdd.path == path)
                self.assertIsInstance(step, osd.Skipped)


if __name__ == "__main__":
    unittest.main()
