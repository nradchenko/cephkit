# cephkit

cephkit is a command-line toolkit for Ceph operators, shaped like `ceph orch`: `cephkit <group> <command>`.
Each command does a job that cephadm and the `ceph` CLI don't do on their own.

The Ceph terms used here and in the docs, such as OSD, BlueStore and cephadm, are explained in the
[Ceph glossary](https://docs.ceph.com/en/latest/glossary/).

## Commands

- `cephkit osd add` creates HDD OSDs whose BlueStore DB lives on mirrored SSDs, one volume per HDD named
  after its serial: [docs/osd-add.md](docs/osd-add.md).

Every command explains itself with `--help`.

## Requirements

- An admin node: one with the `ceph` CLI and an admin keyring.
- Python 3.10 or later, standard library only. Ubuntu 22.04 and later ship it.
- SSH from the admin node to the storage nodes, with the OpenSSH client (`ssh`, `ssh-agent`, `ssh-add`).
  cephkit logs in as cephadm does, with cephadm's user and key: it reads the key from the cluster and holds
  it in memory only for the run. `--ssh-own-key` uses your own SSH keys and config instead. A user other
  than root needs passwordless sudo, as cephadm does.
- Ceph 18.2 (Reef) or 20.2 (Tentacle).

## Installing

From a checkout, with nothing to install:

```
git clone https://github.com/nradchenko/cephkit.git
ln -s "$PWD/cephkit/bin/cephkit" ~/.local/bin/cephkit
cephkit osd add --help
```

## Status

Early. The command output cephkit parses was read in the Ceph, util-linux and LVM2 source of the supported
releases, and the tests run against fixtures written to those formats. It has not yet run against a real
cluster.

## Development

```
uv sync                                      # ruff, pinned in pyproject.toml and uv.lock
uv run ruff format && uv run ruff check
python3 -m unittest discover -s tests
```

## License

Copyright © 2026 Nikita Radchenko

Licensed under the [MIT License](LICENSE).
