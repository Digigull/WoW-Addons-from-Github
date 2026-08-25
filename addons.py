#!/usr/bin/env python3
"""Launcher. The tool itself lives in the `wowaddons` package next to this file.

    python3 addons.py            open the window
    python3 addons.py --help     everything the terminal can do

Kept as a single script at the repo root because that is the path every README,
shell history and muscle memory already has. It does nothing but find the
package and hand over.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run from a checkout, a symlink, or `./addons.py` on $PATH -- all of which can
# leave the repo root off sys.path, and none of which should need PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wowaddons.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main(prog=Path(sys.argv[0]).name or "addons.py")
