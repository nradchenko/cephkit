# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""OSD commands.

`cephkit osd add` creates HDD OSDs whose BlueStore DB is a logical volume on an
md RAID array. See docs/osd-add.md for how it works and why.

The module reads facts, plans from them, and prints the plan. Reading facts is
the only part that talks to the cluster; parsing and planning are pure, so the
tests drive them with captured output.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from cephkit.system import (
    CommandError,
    Host,
    ceph_json,
    ceph_text,
    run_with_heartbeat,
)

ADD_DESCRIPTION = """\
Create HDD OSDs whose BlueStore DB lives on RAID-backed LVM.

Each HDD gets a DB logical volume named db-<serial> in a volume group named
<prefix>-<number> (ssd-1, ssd-2, ...) that sits on an md RAID array. The DB
goes to the group holding the fewest DBs in use. The OSD is created with
`ceph orch daemon add osd`, then checked in the cluster before the next one.

Without --apply, the command prints the plan and changes nothing.

It reads the cluster with `ceph` on this node, and each storage node over SSH
as the user cephadm uses (`ceph cephadm get-user`). With --apply it runs
`lvcreate` on the storage node and `ceph orch daemon add osd` here.
"""

ADD_EPILOG = """\
examples:
  cephkit osd add host1 /dev/sdb                 one OSD on /dev/sdb
  cephkit osd add host1 ABC123                   one OSD on the HDD with serial ABC123
  cephkit osd add host1 --any                    one OSD on the first available HDD
  cephkit osd add host1 --all-available-devices  every available HDD on host1
  cephkit osd add --all-hosts                    every available HDD on every host
  cephkit osd add host1 --any --apply            create it
"""

NO_DB_VGS_HELP = """\
A DB volume group is named {prefix}-<number> and is the only volume group on an
md array with redundancy. For example, with two SSDs /dev/sdX and /dev/sdY:

  mdadm --create /dev/md1 --level=1 --raid-devices=2 /dev/sdX /dev/sdY
  pvcreate /dev/md1
  vgcreate {prefix}-1 /dev/md1

Make sure the array is assembled at boot, as your distribution's mdadm
documentation describes, before putting OSD DBs on it."""

DEFAULT_VG_PREFIX = "ssd"

SIZE_UNITS = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}

# md levels that survive the loss of one member. lsblk reports an md array's
# TYPE as the kernel's md/level string.
REDUNDANT_MD_LEVELS = {"raid1", "raid4", "raid5", "raid6", "raid10"}

# Characters LVM accepts in a volume name. A serial outside this set cannot
# name a DB volume.
LV_NAME_CHARS = re.compile(r"[A-Za-z0-9+_.-]+")

# lsblk: JSON with --bytes makes SIZE a number and ROTA a boolean; empty
# values are null, not "".
LSBLK = [
    "lsblk",
    "--json",
    "--bytes",
    "--nodeps",
    "-o",
    "NAME,PATH,TYPE,SIZE,ROTA,SERIAL",
]

# LVM's JSON report quotes every value, numbers included. Tags are joined by
# the host's report/list_item_separator, so it is pinned here.
LVM_REPORT = [
    "--reportformat",
    "json",
    "--units",
    "b",
    "--nosuffix",
    "--config",
    'report/list_item_separator=","',
]
PVS = ["pvs", *LVM_REPORT, "-o", "pv_name,vg_name"]
VGS = ["vgs", *LVM_REPORT, "-o", "vg_name,vg_free,vg_extent_size"]
LVS = ["lvs", *LVM_REPORT, "-o", "lv_name,vg_name,lv_size,lv_tags"]


def parse_size(text: str) -> int:
    """Parse a size such as 128G into bytes. Units are binary: 1G is 1 GiB."""
    match = re.fullmatch(r"([1-9][0-9]*)([KMGT])", text.strip().upper())
    if not match:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a size: use a whole number and a unit, like 128G"
        )
    number, unit = match.groups()
    return int(number) * SIZE_UNITS[unit]


def parse_vg_prefix(text: str) -> str:
    """Check a DB volume group prefix. ceph-volume claims VGs named ceph*."""
    if text.lower().startswith("ceph"):
        raise argparse.ArgumentTypeError(
            f"{text!r} starts with 'ceph': ceph-volume treats such volume groups "
            "as its own and may carve its own volumes into them"
        )
    return text


def add_commands(groups: argparse._SubParsersAction) -> None:
    osd = groups.add_parser("osd", help="OSD commands")
    commands = osd.add_subparsers(
        title="commands", dest="command", metavar="COMMAND", required=True
    )

    add = commands.add_parser(
        "add",
        help="create HDD OSDs with their DB on RAID-backed LVM",
        description=ADD_DESCRIPTION,
        epilog=ADD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument(
        "host", nargs="?", metavar="HOST", help="the storage node to add OSDs on"
    )
    add.add_argument(
        "device",
        nargs="?",
        metavar="DEVICE",
        help="the HDD for one OSD: /dev/sdX or its serial",
    )
    scope = add.add_mutually_exclusive_group()
    scope.add_argument(
        "--any",
        action="store_true",
        help="one OSD on the first available HDD on HOST, in natural order",
    )
    scope.add_argument(
        "--all-available-devices",
        action="store_true",
        help="one OSD on every available HDD on HOST",
    )
    scope.add_argument(
        "--all-hosts",
        action="store_true",
        help="one OSD on every available HDD on every host that is not offline "
        "or in maintenance",
    )
    add.add_argument(
        "--vg",
        metavar="NAME",
        help="put every DB of this run in this volume group",
    )
    add.add_argument(
        "--vg-prefix",
        type=parse_vg_prefix,
        default=DEFAULT_VG_PREFIX,
        metavar="PREFIX",
        help="DB volume groups are named PREFIX-<number> (default: %(default)s)",
    )
    add.add_argument(
        "--db-size",
        type=parse_size,
        metavar="SIZE",
        help="size of a new DB volume, like 128G (binary units); needed when "
        "the host has no DBs in use, or DBs of different sizes",
    )
    add.add_argument(
        "--ssh-user",
        metavar="USER",
        help="SSH as this user instead of the one cephadm uses",
    )
    add.add_argument(
        "--apply",
        action="store_true",
        help="carry out the plan; without it nothing changes",
    )
    add.set_defaults(run=run_add, parser=add)


def check_add_scope(args: argparse.Namespace) -> str | None:
    """Return why the host, device and scope flags don't form one of the
    four modes, or None when they do."""
    if args.all_hosts:
        if args.host or args.device:
            return "--all-hosts takes no HOST or DEVICE"
        return None
    if not args.host:
        return "give a HOST, or --all-hosts"
    chosen = [args.device is not None, args.any, args.all_available_devices]
    if sum(chosen) != 1:
        return "give exactly one of DEVICE, --any or --all-available-devices"
    return None


@dataclass(frozen=True)
class Hdd:
    """An HDD the orchestrator reports as available, joined with lsblk."""

    path: str
    serial: str | None
    device_id: str
    size: int


@dataclass(frozen=True)
class DbVg:
    """A DB volume group: <prefix>-<number>, alone on a redundant md array."""

    name: str
    number: int
    free: int
    extent: int
    md: str  # the array's kernel name, as OSD metadata reports it: md1


@dataclass(frozen=True)
class DbLv:
    """A logical volume in a DB volume group, with what ceph-volume tagged on it."""

    vg: str
    name: str
    size: int
    kind: str | None  # ceph.type: "db", "wal", "block", or None when untagged
    osd_id: int | None  # the OSD that took it, as ceph-volume tagged it

    @property
    def is_db_in_use(self) -> bool:
        return self.kind == "db" and self.osd_id is not None


@dataclass(frozen=True)
class HostFacts:
    name: str
    hdds: list[Hdd]  # in natural order: sdb before sdaa
    vgs: list[DbVg]
    lvs: list[DbLv]


class PlanError(Exception):
    """The host can't be planned for at all; the message says what to fix."""


def lvm_report(data: dict, kind: str) -> list[dict]:
    """The rows of an LVM JSON report: {"report": [{"lv": [...]}]}."""
    return data["report"][0][kind]


def key_values(text: str) -> dict[str, str]:
    """Parse "a=1,b=2". Used for LVM tags (comma-joined as pinned in
    LVM_REPORT; a tag can't contain a comma; ceph-volume writes key=value)
    and for RocksDB option strings."""
    return dict(item.split("=", 1) for item in text.split(",") if "=" in item)


def is_available_hdd(device: dict) -> bool:
    """A device from `ceph orch device ls --format json` that can take an OSD.
    human_readable_type is "hdd" when sysfs rotational is "1"."""
    return device["available"] and device["human_readable_type"] == "hdd"


def extent_rounded(size: int, extent: int) -> int:
    """What lvcreate allocates for `size`: whole extents, rounded up."""
    return -(-size // extent) * extent


def host_facts(
    name: str,
    inventory: dict,
    lsblk: dict,
    pvs: dict,
    vgs: dict,
    lvs: dict,
    vg_prefix: str,
) -> HostFacts:
    """Join one host's orchestrator inventory with what lsblk and LVM say there.

    `inventory` is the host's entry from `ceph orch device ls --format json`.
    A device is an HDD when human_readable_type is "hdd", which the inventory
    derives from sysfs rotational being "1".
    """
    blockdevices = {dev["path"]: dev for dev in lsblk["blockdevices"]}

    hdds = []
    for dev in inventory["devices"]:
        if not is_available_hdd(dev):
            continue
        seen = blockdevices.get(dev["path"], {})
        hdds.append(
            Hdd(
                path=dev["path"],
                serial=seen.get("serial"),
                device_id=dev["device_id"],
                size=int(dev["sys_api"]["size"]),
            )
        )
    # Kernel names grow in length before they grow in value: sdz, then sdaa.
    hdds.sort(key=lambda hdd: (len(hdd.path), hdd.path))

    pattern = re.compile(re.escape(vg_prefix) + r"-([0-9]+)")
    pvs_of: dict[str, list[str]] = {}
    for pv in lvm_report(pvs, "pv"):
        pvs_of.setdefault(pv["vg_name"], []).append(pv["pv_name"])

    db_vgs = []
    for vg in lvm_report(vgs, "vg"):
        match = pattern.fullmatch(vg["vg_name"])
        if not match:
            continue
        pv_names = pvs_of.get(vg["vg_name"], [])
        levels = [blockdevices.get(pv, {}).get("type") for pv in pv_names]
        if len(pv_names) != 1 or levels[0] not in REDUNDANT_MD_LEVELS:
            raise PlanError(
                f"{vg['vg_name']} is on {', '.join(pv_names) or 'no PV'} "
                f"({', '.join(str(level) for level in levels) or 'nothing'}): "
                "a DB volume group must be the only one on an md array with "
                "redundancy"
            )
        db_vgs.append(
            DbVg(
                name=vg["vg_name"],
                number=int(match.group(1)),
                free=int(vg["vg_free"]),
                extent=int(vg["vg_extent_size"]),
                md=pv_names[0].removeprefix("/dev/"),
            )
        )
    # LVM lists VGs sorted as strings (ssd-10 before ssd-2); people count.
    db_vgs.sort(key=lambda db_vg: db_vg.number)
    if not db_vgs:
        raise PlanError(
            f"no DB volume groups on {name}.\n\n"
            + NO_DB_VGS_HELP.format(prefix=vg_prefix)
        )

    db_vg_names = {vg.name for vg in db_vgs}
    db_lvs = []
    for lv in lvm_report(lvs, "lv"):
        if lv["vg_name"] not in db_vg_names:
            continue
        tags = key_values(lv["lv_tags"])
        # The same test ceph-volume uses to call a DB or WAL volume taken.
        taken = tags.get("ceph.type") in ("db", "wal") and "ceph.osd_id" in tags
        db_lvs.append(
            DbLv(
                vg=lv["vg_name"],
                name=lv["lv_name"],
                size=int(lv["lv_size"]),
                kind=tags.get("ceph.type"),
                osd_id=int(tags["ceph.osd_id"]) if taken else None,
            )
        )

    return HostFacts(name=name, hdds=hdds, vgs=db_vgs, lvs=db_lvs)


@dataclass(frozen=True)
class NewOsd:
    hdd: Hdd
    vg: str
    lv: str
    create_lv: bool
    why: str


@dataclass(frozen=True)
class Skipped:
    hdd: Hdd
    why: str


@dataclass(frozen=True)
class HostPlan:
    host: str
    db_size: int
    db_size_why: str
    steps: list[NewOsd | Skipped]


def choose_db_size(facts: HostFacts, requested: int | None) -> tuple[int, str]:
    if requested:
        return requested, "given with --db-size"
    sizes = Counter(lv.size for lv in facts.lvs if lv.is_db_in_use)
    if len(sizes) == 1:
        size, count = sizes.popitem()
        return size, f"the size of the {count} DBs in use on {facts.name}"
    if not sizes:
        raise PlanError(
            f"no DBs in use on {facts.name} to take the size from: give --db-size"
        )
    found = ", ".join(
        f"{count} of {human(size)}" for size, count in sorted(sizes.items())
    )
    raise PlanError(
        f"the DBs in use on {facts.name} differ in size ({found}): give --db-size"
    )


def plan_host(
    facts: HostFacts,
    *,
    device: str | None = None,
    only_one: bool = False,
    db_size: int | None = None,
    vg: str | None = None,
) -> HostPlan:
    """Plan OSDs for the host's available HDDs.

    `device` limits the plan to one HDD, by path or serial; `only_one` stops at
    the first HDD that can be planned. Every HDD that can't be planned is in
    the plan with the reason.
    """
    size, size_why = choose_db_size(facts, db_size)
    if vg and vg not in {db_vg.name for db_vg in facts.vgs}:
        raise PlanError(f"--vg {vg} is not a DB volume group on {facts.name}")

    hdds = facts.hdds
    if device:
        hdds = [hdd for hdd in hdds if device in (hdd.path, hdd.serial)]
        if not hdds:
            available = ", ".join(hdd.path for hdd in facts.hdds) or "none"
            raise PlanError(
                f"{device} is not an available HDD on {facts.name} "
                f"(available: {available})"
            )

    taken = Counter(lv.vg for lv in facts.lvs if lv.is_db_in_use)
    free = {db_vg.name: db_vg.free for db_vg in facts.vgs}
    extent = {db_vg.name: db_vg.extent for db_vg in facts.vgs}
    planned: dict[str, str] = {}  # DB volume name -> HDD path, in this run
    steps: list[NewOsd | Skipped] = []
    for hdd in hdds:
        step = plan_hdd(hdd, facts, size, vg, taken, free, planned)
        steps.append(step)
        if isinstance(step, NewOsd):
            taken[step.vg] += 1
            planned[step.lv] = hdd.path
            if step.create_lv:
                free[step.vg] -= extent_rounded(size, extent[step.vg])
            if only_one:
                break
    return HostPlan(facts.name, size, size_why, steps)


def plan_hdd(
    hdd: Hdd,
    facts: HostFacts,
    size: int,
    pinned_vg: str | None,
    taken: Counter,
    free: dict[str, int],
    planned: dict[str, str],
) -> NewOsd | Skipped:
    if not hdd.serial:
        return Skipped(hdd, "lsblk reports no serial, so its DB can't be named")
    if not LV_NAME_CHARS.fullmatch(hdd.serial):
        return Skipped(hdd, f"serial {hdd.serial!r} can't be part of an LVM name")
    # The inventory is a cached scan. If sdX names moved since, the inventory's
    # device_id for this path belongs to another disk.
    if not hdd.device_id.endswith("_" + hdd.serial):
        return Skipped(
            hdd,
            f"the inventory says {hdd.device_id} but lsblk reports serial "
            f"{hdd.serial}: refresh it with `ceph orch device ls --refresh`",
        )

    name = f"db-{hdd.serial}"
    if name in planned:
        return Skipped(
            hdd, f"{planned[name]} has the same serial and is planned already"
        )
    copies = [lv for lv in facts.lvs if lv.name == name]
    for lv in copies:
        if lv.osd_id is not None:
            return Skipped(hdd, f"{lv.vg}/{name} is already the DB of osd.{lv.osd_id}")
    if len(copies) > 1:
        where = ", ".join(lv.vg for lv in copies)
        return Skipped(hdd, f"{name} exists in {where}: remove the stale copies first")
    if copies:
        lv = copies[0]
        if pinned_vg and lv.vg != pinned_vg:
            return Skipped(hdd, f"{lv.vg}/{name} exists, but --vg asks for {pinned_vg}")
        if lv.size != size:
            return Skipped(
                hdd, f"{lv.vg}/{name} exists with {human(lv.size)}, not {human(size)}"
            )
        return NewOsd(
            hdd, lv.vg, name, create_lv=False, why="exists and no OSD uses it"
        )

    def room(db_vg: DbVg) -> bool:
        return free[db_vg.name] >= extent_rounded(size, db_vg.extent)

    def rank(db_vg: DbVg) -> tuple[int, int]:
        return taken[db_vg.name], db_vg.number

    allowed = [db_vg for db_vg in facts.vgs if pinned_vg in (None, db_vg.name)]
    fits = [db_vg for db_vg in allowed if room(db_vg)]
    if not fits:
        if len(allowed) == 1:
            only = allowed[0].name
            return Skipped(
                hdd, f"no room for {human(size)} in {only} ({human(free[only])} free)"
            )
        best = max(allowed, key=lambda db_vg: (free[db_vg.name], -db_vg.number))
        return Skipped(
            hdd,
            f"no room for {human(size)} in any of {len(allowed)} DB volume groups "
            f"(most free: {best.name}, {human(free[best.name])})",
        )
    chosen = min(fits, key=rank)
    if pinned_vg:
        return NewOsd(hdd, chosen.name, name, create_lv=True, why="pinned with --vg")

    why = f"fewest DBs in use ({taken[chosen.name]})"
    # Full VGs that would have won the choice: they are why it went elsewhere.
    passed_over = [
        db_vg.name for db_vg in sorted(allowed, key=rank) if rank(db_vg) < rank(chosen)
    ]
    if len(passed_over) == 1:
        why += f"; {passed_over[0]} has only {human(free[passed_over[0]])} free"
    elif passed_over:
        more = len(passed_over) - 2
        named = ", ".join(passed_over[:2]) + (f" and {more} more" if more else "")
        why += f"; {named} are too full"
    return NewOsd(hdd, chosen.name, name, create_lv=True, why=why)


def human(size: float) -> str:
    for unit, factor in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}".replace(".0 ", " ")
    return f"{int(size)} B"


def rocksdb_levels(size: int, options: str) -> str | None:
    """How many RocksDB levels fit whole in a DB of `size`, from the cluster's
    bluestore_rocksdb_options (`ceph config get` prints it as plain text)."""
    settings = key_values(options)
    try:
        level = int(settings["max_bytes_for_level_base"])
        multiplier = int(settings["max_bytes_for_level_multiplier"])
    except (KeyError, ValueError):
        return None
    total, held = 0, 0
    while total + level <= size:
        total += level
        held += 1
        level *= multiplier
    if not held:
        return "holds no RocksDB level whole"
    return f"holds RocksDB levels L1-L{held} whole ({human(total)})"


def format_plan(plan: HostPlan, rocksdb_options: str) -> list[str]:
    planned = [step for step in plan.steps if isinstance(step, NewOsd)]
    size = human(plan.db_size)
    header = f"{plan.host}: {len(planned)} OSD(s) planned, DB size {size} ({plan.db_size_why})"
    lines = [header]
    for step in plan.steps:
        hdd = step.hdd
        head = f"  {hdd.path:<10} {hdd.serial or '-':<14} {human(hdd.size):>9}"
        if isinstance(step, NewOsd):
            action = "create" if step.create_lv else "reuse"
            lines.append(f"{head}  -> {step.vg}/{step.lv}  {action}: {step.why}")
        else:
            lines.append(f"{head}  skipped: {step.why}")
    if planned:
        shares = sorted({plan.db_size / step.hdd.size * 100 for step in planned})
        share = f"{shares[0]:.2f}%"
        if len(shares) > 1:
            share = f"{shares[0]:.2f}-{shares[-1]:.2f}%"
        levels = rocksdb_levels(plan.db_size, rocksdb_options)
        summary = f"  each DB is {share} of its HDD"
        if levels:
            summary += f" and {levels}"
        lines.append(summary)
    return lines


def gather_host(host: Host, inventory: dict, vg_prefix: str) -> HostFacts:
    return host_facts(
        host.name,
        inventory,
        lsblk=json.loads(host.run(LSBLK)),
        pvs=json.loads(host.run(PVS, root=True)),
        vgs=json.loads(host.run(VGS, root=True)),
        lvs=json.loads(host.run(LVS, root=True)),
        vg_prefix=vg_prefix,
    )


BOOT_TIMEOUT = 5 * 60  # seconds; a new OSD usually reports in 2-3 minutes
POLL_EVERY = 10
HEARTBEAT_EVERY = 30


class ApplyStop(Exception):
    """A step did not end as planned. The message says what went wrong; the
    operator investigates, and running the same command again resumes."""


def stamp() -> str:
    """The time at the start of every apply log line."""
    return time.strftime("%H:%M:%S")


def elapsed(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


class HostActions:
    """Everything --apply does to one host and to the cluster. The tests
    replace it with a scripted stand-in."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def serial(self, path: str) -> str | None:
        return json.loads(self.host.run([*LSBLK, path]))["blockdevices"][0]["serial"]

    def create_lv(self, vg: str, lv: str, size: int) -> None:
        # No --yes, and stdin is /dev/null: when LVM finds an old signature its
        # prompt reads "no", so lvcreate stops and removes the new LV. The
        # operator decides what to do. --wipesignatures y is explicit because
        # otherwise lvm.conf decides whether LVM looks for signatures at all.
        # One gap: with allocation/use_blkid_wiping=0 LVM knows only md, swap
        # and LUKS signatures, so a Ceph label goes unseen and --zero y
        # overwrites its first 4 KiB. No data is lost: a new LV only gets free
        # extents.
        self.host.run(
            ["lvcreate", "--wipesignatures", "y", "--zero", "y"]
            + ["-n", lv, "-L", f"{size}b", vg],
            root=True,
        )

    def add_osd(
        self, path: str, vg: str, lv: str, tick: Callable[[float], None]
    ) -> None:
        # cephadm waits for ceph-volume and the deploy, up to its own 15
        # minutes, so there is no time limit here.
        spec = f"{self.host.name}:data_devices={path},db_devices=/dev/{vg}/{lv}"
        run_with_heartbeat(
            ["ceph", "orch", "daemon", "add", "osd", spec], tick, HEARTBEAT_EVERY
        )

    def lv_tags(self, vg: str, lv: str) -> dict[str, str]:
        """The tags on one volume. Fails when the volume does not exist."""
        rows = lvm_report(
            json.loads(self.host.run([*LVS, f"{vg}/{lv}"], root=True)), "lv"
        )
        return key_values(rows[0]["lv_tags"])

    def lv_state(self, vg: str, lv: str) -> str:
        """The volume as it is now, for the operator after a stop.

        What a failure leaves depends on where it happened and on the release.
        On 18.2, ceph-volume's rollback runs `zap --destroy --osd-id`, which
        removes the DB volume, or its whole VG and PV when it was the last
        volume there. On 20.2 the rollback leaves the volume tagged for an OSD
        id that was purged. A deploy failure after a good prepare leaves it
        tagged for an OSD that exists but is down. So it is read, not assumed."""
        try:
            tags = self.lv_tags(vg, lv)
        except CommandError as error:
            return f"can't be read: {error}"
        if "ceph.osd_id" in tags:
            return f"exists, tagged for osd.{tags['ceph.osd_id']}"
        return "exists, untagged"

    def osd_info(self, osd_id: int) -> dict:
        """The OSD's entry in the OSD map: "uuid", and "up" as 0 or 1."""
        return ceph_json("osd", "info", f"osd.{osd_id}")

    def osd_metadata(self, osd_id: int) -> dict:
        """What the OSD reported when it last booted. The monitor keeps it
        across `osd destroy`, so it is read only once the OSD is up."""
        return ceph_json("osd", "metadata", str(osd_id))

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def apply_step(
    step: NewOsd,
    db_size: int,
    facts: HostFacts,
    actions: HostActions,
    say: Callable[[str], None],
) -> int:
    """Create one planned OSD and check it in the cluster. Returns its id."""
    hdd = step.hdd
    where = f"{step.vg}/{step.lv}"

    serial = actions.serial(hdd.path)
    if serial != hdd.serial:
        raise ApplyStop(
            f"{hdd.path} now reports serial {serial}, not {hdd.serial}. "
            "Nothing was changed for it; run the command again to plan anew"
        )

    if step.create_lv:
        try:
            actions.create_lv(step.vg, step.lv, db_size)
        except CommandError as error:
            raise ApplyStop(f"lvcreate failed: {error}") from None
        say(f"created {where}")
    else:
        say(f"reusing {where}")

    say("ceph orch daemon add osd (cephadm allows 15m)")
    started = actions.now()
    try:
        actions.add_osd(
            hdd.path, step.vg, step.lv, lambda s: say(f"still running ({elapsed(s)})")
        )
    except CommandError as error:
        raise ApplyStop(
            f"`ceph orch daemon add osd` failed: {error}. See `ceph log last cephadm`"
        ) from None

    # cephadm has waited for ceph-volume, so the tags are final now. 20.2
    # exits 0 even when ceph-volume failed, which only this check catches.
    tags = actions.lv_tags(step.vg, step.lv)
    if tags.get("ceph.type") != "db" or "ceph.osd_id" not in tags:
        raise ApplyStop(
            f"ceph-volume did not take {where}: it has no ceph.osd_id tag. "
            "cephadm may report this only in its log: see `ceph log last cephadm`"
        )
    osd_id = int(tags["ceph.osd_id"])
    osd_fsid = tags.get("ceph.osd_fsid", "")
    say(
        f"osd.{osd_id} prepared ({elapsed(actions.now() - started)}), "
        f"waiting for it to boot (up to {BOOT_TIMEOUT // 60}m)"
    )

    # Up with our uuid means the new OSD booted: the monitor records its
    # metadata and marks it up in one step. An id reused after `osd destroy`
    # still shows the old OSD's metadata until then.
    waiting_since = actions.now()
    last_heartbeat = waiting_since
    last_error = None
    while True:
        try:
            info = actions.osd_info(osd_id)
            if info["up"] == 1 and info["uuid"] == osd_fsid:
                break
        except CommandError as error:
            last_error = error  # a mon election or mgr failover passes
        if actions.now() - waiting_since >= BOOT_TIMEOUT:
            detail = f" (last error: {last_error})" if last_error else ""
            raise ApplyStop(
                f"osd.{osd_id} is not up after {BOOT_TIMEOUT // 60}m{detail}. "
                f"Check `ceph orch ps {facts.name}` and `ceph log last cephadm`"
            )
        actions.sleep(POLL_EVERY)
        if actions.now() - last_heartbeat >= HEARTBEAT_EVERY:
            last_heartbeat = actions.now()
            say(f"still waiting ({elapsed(last_heartbeat - waiting_since)})")

    metadata = actions.osd_metadata(osd_id)
    md = next(db_vg.md for db_vg in facts.vgs if db_vg.name == step.vg)
    device_ids = [
        item.split("=", 1)[-1] for item in metadata.get("device_ids", "").split(",")
    ]
    problems = []
    if hdd.device_id not in device_ids:
        problems.append(
            f"its devices are {metadata.get('device_ids')!r}, not {hdd.device_id}"
        )
    if metadata.get("bluefs_dedicated_db") != "1":
        problems.append("it has no dedicated DB")
    elif metadata.get("bluefs_db_devices") != md:
        problems.append(f"its DB is on {metadata.get('bluefs_db_devices')}, not {md}")
    if problems:
        raise ApplyStop(
            f"osd.{osd_id} is up, but {'; '.join(problems)}. It needs a person: "
            f"compare `ceph osd metadata {osd_id}` with the plan"
        )
    say(f"osd.{osd_id} up with its DB on {md} ({elapsed(actions.now() - started)})")
    return osd_id


def run_add(args: argparse.Namespace) -> int:
    problem = check_add_scope(args)
    if problem:
        args.parser.error(problem)
    print(f"cephkit osd add, {time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        user = args.ssh_user or ceph_text("cephadm", "get-user")
        hosts = ceph_json("orch", "host", "ls")
        inventory = {
            entry["name"]: entry for entry in ceph_json("orch", "device", "ls")
        }
        rocksdb_options = ceph_text("config", "get", "osd", "bluestore_rocksdb_options")
    except CommandError as error:
        print(f"cephkit: {error}", file=sys.stderr)
        return 1

    skipped_hosts = []
    if args.all_hosts:
        targets = []
        for entry in sorted(hosts, key=lambda entry: entry["hostname"]):
            status = entry["status"].lower()
            if status in ("maintenance", "offline"):
                skipped_hosts.append(f"{entry['hostname']}: {status}")
            else:
                targets.append(entry)
    else:
        targets = [entry for entry in hosts if entry["hostname"] == args.host]
        if not targets:
            print(
                f"cephkit: {args.host} is not in `ceph orch host ls`", file=sys.stderr
            )
            return 1

    wants_one = bool(args.device or args.any)
    failed = False
    plans: list[tuple[Host, HostFacts, HostPlan]] = []
    for entry in targets:
        host = Host(entry["hostname"], entry["addr"], user)
        devices = inventory.get(host.name, {"devices": []})
        # Hosts without a free HDD (mons, gateways, full OSD hosts) need no SSH.
        if not any(is_available_hdd(device) for device in devices["devices"]):
            if args.all_hosts:
                skipped_hosts.append(f"{host.name}: no available HDD")
                continue
            print(f"\n{host.name}: no available HDD")
            failed = wants_one
            continue
        try:
            facts = gather_host(host, devices, args.vg_prefix)
            plan = plan_host(
                facts,
                device=args.device,
                only_one=args.any,
                db_size=args.db_size,
                vg=args.vg,
            )
        except (CommandError, PlanError) as error:
            if not args.all_hosts:
                print(f"cephkit: {error}", file=sys.stderr)
                return 1
            skipped_hosts.append(f"{host.name}: {error}")
            failed = True
            continue
        # A blank line before each host's block, here and in the apply log.
        print()
        print("\n".join(format_plan(plan, rocksdb_options)))
        plans.append((host, facts, plan))
        # The one OSD the operator asked for can't be done: say so in the status.
        if wants_one and not any(isinstance(step, NewOsd) for step in plan.steps):
            failed = True

    if skipped_hosts:
        print()
    for line in skipped_hosts:
        print(f"skipped host {line}")
    if not args.apply:
        print("\nNothing changed: add --apply to carry this out.")
        return 1 if failed else 0

    created = 0
    for host, facts, plan in plans:
        steps = [step for step in plan.steps if isinstance(step, NewOsd)]
        if steps:
            print()
        actions = HostActions(host)
        for step in steps:
            prefix = f"{host.name} {step.hdd.path} {step.hdd.serial}"

            def say(text: str, prefix: str = prefix) -> None:
                print(f"{stamp()} {prefix}: {text}", flush=True)

            try:
                apply_step(step, plan.db_size, facts, actions, say)
            except (ApplyStop, CommandError) as error:
                say(f"stopped: {error}")
                # What is there now, not what should be: a failed step may have
                # left the volume tagged, or ceph-volume may have removed it.
                say(f"{step.vg}/{step.lv} {actions.lv_state(step.vg, step.lv)}")
                print(f"\n{stamp()} {created} OSD(s) created before the stop")
                return 1
            created += 1
    print(f"\n{stamp()} {created} OSD(s) created")
    return 1 if failed else 0
