#!/usr/bin/env python3
"""Run MyProfiler from a checkout: python3 tools/MyProfiler/myprofiler.py report profile.json"""

import sys

from myprofiler.cli import main

sys.exit(main())
