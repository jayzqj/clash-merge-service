#!/usr/bin/env python3
"""Entry point: python -m main  (or: python main.py).

Listens on 127.0.0.1:18080 by default (env PORT). nginx proxies
/admin and /api/* to it; the YAML output is served by nginx as a
static file, not by this process.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web import main

if __name__ == "__main__":
    main()
