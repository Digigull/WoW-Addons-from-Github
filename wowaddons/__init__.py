"""Point a WoW client at whatever repos you want your addons to come from.

The package is split three ways so the two front ends cannot drift apart:

    core    the engine -- manifest, scanning, sources, install. Never prints.
    cli     argparse and the terminal output
    gui     the Tkinter window

Everything that decides *what happens* lives in `core`; `cli` and `gui` only
decide how it is shown. In particular both call `core.update_addon` for one
addon at a time, so the rule that one failure does not sink the run is written
once rather than once per front end.
"""

# Single source of truth. `--version` reports it, the window puts it in its
# title bar, and the release tag is expected to match -- a test checks that the
# tag being built is the version being shipped, because a binary that lies
# about which build it is makes every bug report start from nothing.
__version__ = "0.3.0"
