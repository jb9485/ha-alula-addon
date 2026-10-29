"""python -m alula_bridge"""
from __future__ import annotations

import asyncio
import os
import sys

from .bridge import main


if __name__ == "__main__":
    path = os.environ.get("OPTIONS_PATH", "/data/options.json")
    try:
        asyncio.run(main(path))
    except KeyboardInterrupt:
        sys.exit(0)
