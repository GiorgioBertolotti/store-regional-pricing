#!/usr/bin/env python3
"""Single entry point for store-regional-pricing.

Run with no arguments for the guided flow, or see `python pricing.py --help` for
subcommands (setup, doctor, scale, apply, offer, refresh-data).
"""

import sys

from store_pricing.cli import main

if __name__ == "__main__":
    sys.exit(main())
