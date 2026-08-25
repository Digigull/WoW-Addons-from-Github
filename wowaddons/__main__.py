"""`python -m wowaddons` -- the window when given nothing, the CLI when given arguments.

This is also what the AppImage runs, which is why the no-arguments case has to
be the GUI: an AppImage is double-clicked far more often than it is typed.
"""

from __future__ import annotations

import sys

from . import cli


def main(argv: list[str] | None = None, *, prog: str = "addons.py") -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        cli.main(argv, prog=prog)
        return

    # No arguments: someone double-clicked, or ran it bare. Open the window.
    try:
        from . import gui
    except ImportError as exc:
        # Tkinter is in the standard library but distributions package it
        # separately, so a bare `python3 -m wowaddons` on a server can land
        # here. Say which package to install rather than showing a traceback.
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
