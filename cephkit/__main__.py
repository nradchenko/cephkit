# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nikita Radchenko
"""Lets `python3 -m cephkit` run the command line."""

import sys

from cephkit.cli import main

sys.exit(main())
