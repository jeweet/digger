#!/usr/bin/env python3
"""Start Digger from a source checkout: python3 run.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from digger.app import main  # noqa: E402

sys.exit(main())
