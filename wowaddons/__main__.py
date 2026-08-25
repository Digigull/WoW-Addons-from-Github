"""`python -m wowaddons` -- the window when given nothing, the CLI when given arguments.

This is what both packaged builds run, which is why the no-arguments case has to
be the GUI: an AppImage and a .exe are double-clicked far more often than typed.

The Windows build is `--windowed`, so it starts with no console and no usable
stdout. Anything that prints -- which is every CLI path, starting with argparse
handling --help -- has to acquire one first. See winconsole.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import cli, winconsole


def frozen() -> bool:
    """True inside a PyInstaller build, where sys.executable IS the program."""
    return getattr(sys, "frozen", False)


def invoked_as(default: str = "addons.py") -> str:
    """What to call this in --help.

    Inside an AppImage the thing the user actually typed is the .AppImage file,
    whose path the runtime puts in $APPIMAGE; by then argv[0] is a path into a
    mount point under /tmp and would be nonsense in a usage line. In a frozen
    Windows build the same is true of sys.executable, which is the .exe itself.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return Path(appimage).name
    if frozen():
        return Path(sys.executable).name
    return default


def main(argv: list[str] | None = None, *, prog: str | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    prog = invoked_as() if prog is None else invoked_as(prog)
    if argv:
        # Before argparse, not after: --help writes to stdout, and in a windowed
        # build there is no stdout to write to until this has run.
        winconsole.ensure_output()
        cli.main(argv, prog=prog)
        return

    # No arguments: someone double-clicked, or ran it bare. Open the window.
    try:
        from . import gui
    except ImportError as exc:
        # Tkinter is in the standard library but distributions package it
        # separately, so a bare `python3 -m wowaddons` on a server can land
        # here. Say which package to install rather than showing a traceback.
        #
        # A packaged build should never reach this -- both bundle their own Tk --
        # but if one ever did, failing silently with no window and no message is
        # the worst of all outcomes, so make somewhere for this to be read.
        winconsole.ensure_output()
        print(
            f"the window needs Tkinter, which this Python does not have ({exc}).\n"
            "  Debian/Ubuntu:  sudo apt install python3-tk\n"
            "  Fedora:         sudo dnf install python3-tkinter\n"
            "  Arch:           sudo pacman -S tk\n"
            "Or use the terminal instead -- the commands are below.\n",
            file=sys.stderr,
        )
        cli.build_parser(prog).print_help()
        raise SystemExit(2)
    gui.main()


if __name__ == "__main__":
    main()
