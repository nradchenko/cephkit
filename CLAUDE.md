# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`cephkit` is a command-line toolkit for operating Ceph clusters, shaped like `ceph orch`: `cephkit <group> <command> [args]`.

## Principles

- **Python 3.10 or later, standard library only.** Python 3.10 is what Ubuntu 22.04 ships. `cephkit` runs on an admin node: one with the `ceph` CLI and an admin keyring.
- **Two ways to run it, one code base.** A release is a single executable file built with `zipapp`. A checkout runs through the entry script `bin/cephkit`, which works when symlinked into `PATH`. Neither needs installing.
- **One module per command group** (`cephkit/osd.py`, …), readable top to bottom for one flow. Code that several groups need, such as running `ceph` or SSH, lives in its own module.
- **Read the cluster as JSON.** Call `ceph`, `rados`, `rbd` or `radosgw-admin` with `--format json` through `subprocess` and parse it with `json`. Never parse the human-readable output: it changes between releases. A command that prints one bare value whatever `--format` says, such as `ceph cephadm get-user` or `ceph config get <who> <option>`, is read as that value.
- **A command that changes things in several steps plans by default and acts with `--apply`.** Without it, it prints what it would do and why, and changes nothing. With it, each step is checked against the cluster before the next one starts: a tool's own success message is not the check. A command that only reads, or makes one change, acts directly.
- **Storage nodes are reached over SSH the way cephadm reaches them:** the user from `ceph cephadm get-user`, the address from `ceph orch host ls`, `sudo -n` when the user is not root, and cephadm's key. The key goes from `ceph config-key get` into a private ssh-agent that cephkit starts and always stops: never on disk, never forwarded. In this mode `ssh` ignores every ssh_config and offers that key only. A host's key is accepted on first contact into the operator's `known_hosts`, and a changed one is refused. `--ssh-own-key` (or `CEPHKIT_SSH=own`) leaves keys and host keys to the operator's own SSH setup.
- **Supported Ceph releases: 18.2 (Reef) and 20.2 (Tentacle).** A fact about Ceph's behaviour is checked in the source of each supported release.
- `--help` says what a command does and what it touches.
- Every Python source file, `bin/cephkit` included, starts with the licence header, after the shebang line if it has one:
  ```python
  # SPDX-License-Identifier: MIT
  # Copyright (c) <year> <copyright holder>
  ```

## Docs

- `README.md` covers the whole toolkit and gives one prose line per command; each command's doc in `docs/` owns everything about that command.
- The Ceph glossary explains Ceph terms: the README sends readers there once, for whenever they want to learn more about one.

## Development

Development tools are pinned in `pyproject.toml` and locked in `uv.lock`; `cephkit` itself needs neither.

- Set up: `uv sync`
- Format, then lint: `uv run ruff format && uv run ruff check`
- Build the release file: `python3 tools/build.py --version 0.1.0 --output dist/cephkit`. It is reproducible, and the version exists only in the built file: the git tag is its one source.
- Release: push a tag `vX.Y.Z`; `.github/workflows/release.yml` builds, tests the built file, and publishes it with `SHA256SUMS`.
- CI (`.github/workflows/ci.yml`) runs the same gate on Python 3.10 and 3.12, pinned to exact patch releases, and the wiring tests against a built release file. It reads ruff's version and hash from `uv.lock`, so updating ruff with uv updates CI too. Actions are pinned to full commit SHAs.

## Tests

- **Few tests, each for a risk that would hurt an operator.** Keep the project simple: no tests for plumbing or for what argparse and the standard library already guarantee.
- `unittest`, fed with JSON captured from real clusters and stored under `tests/fixtures/`, each file named with the Ceph release it came from. No test talks to a live cluster.
- Scrub a fixture before committing it: hostnames, IP addresses, fsids, pool, bucket and user names.
- `tests/fixtures/ceph-*` are real captures, trimmed to what cephkit reads and scrubbed: they pin the output formats. `tests/fixtures/synthetic-*` are small hand-written scenarios in those formats: tagged, duplicate or full volumes that a real capture rarely shows. A difference between a real capture and what the code expects is a finding.
- Run all tests: `python3 -m unittest discover -s tests`
- Run one: `python3 -m unittest tests.test_<module>.<Class>.<method>`
