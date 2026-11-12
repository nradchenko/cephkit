# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`cephkit` is a command-line toolkit for operating Ceph clusters, shaped like `ceph orch`: `cephkit <group> <command> [args]`.

## Principles

- **Python 3.10 or later, standard library only.** Python 3.10 is what Ubuntu 22.04 ships. `cephkit` runs on an admin node: one with the `ceph` CLI and an admin keyring.
- **Two ways to run it, one code base.** A release is a single executable file built with `zipapp`. A checkout runs through the `cephkit` entry script at the top of the repository, which works when symlinked into `PATH`. Neither needs installing.
- **One module per command group** (`cephkit/osd.py`, …), readable top to bottom for one flow. Code that several groups need, such as running `ceph` or SSH, lives in its own module.
- **Read the cluster as JSON.** Call `ceph`, `rados`, `rbd` or `radosgw-admin` with `--format json` through `subprocess` and parse it with `json`. Never parse the human-readable output: it changes between releases.
- **A command that changes things in several steps plans by default and acts with `--apply`.** Without it, it prints what it would do and why, and changes nothing. With it, each step is checked against the cluster before the next one starts: a tool's own success message is not the check. A command that only reads, or makes one change, acts directly.
- **Storage nodes are reached over SSH the way cephadm reaches them:** the user from `ceph cephadm get-user`, the address from `ceph orch host ls`, and `sudo -n` when the user is not root. The operator's own key authenticates, and host keys are checked as the operator's SSH config says.
- **Supported Ceph releases: 18.2 (Reef) and 20.2 (Tentacle).** A fact about Ceph's behaviour is checked in the source of each supported release.
- `--help` says what a command does and what it touches.
- Every source file starts with the licence header, after the shebang line if it has one:
  ```python
  # SPDX-License-Identifier: MIT
  # Copyright (c) <year> <copyright holder>
  ```

## Development

Development tools are pinned in `pyproject.toml` and locked in `uv.lock`; `cephkit` itself needs neither.

- Set up: `uv sync`
- Format, then lint: `uv run ruff format && uv run ruff check`

## Tests

- `unittest`, fed with JSON captured from real clusters and stored under `tests/fixtures/`, each file named with the Ceph release it came from. No test talks to a live cluster.
- Scrub a fixture before committing it: hostnames, IP addresses, fsids, pool, bucket and user names.
- Run all tests: `python3 -m unittest discover -s tests`
- Run one: `python3 -m unittest tests.test_<module>.<Class>.<method>`
