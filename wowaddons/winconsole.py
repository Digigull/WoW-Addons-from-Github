"""Give a windowed Windows build somewhere to write, when it was given arguments.

The Windows binary is built with PyInstaller's `--windowed`, because the whole
point of it is that double-clicking opens a window rather than a black console
box. The cost is that the process starts with no console at all: Python finds
no valid standard handles, sets `sys.stdout` and `sys.stderr` to None, and the
first thing argparse does on `--help` is crash trying to write to them.

So the same binary has to be able to grow a console on demand. `ensure_output`
is called only when there are command-line arguments -- the GUI path never
touches any of this, and neither does any other platform.

The order below is the whole design, and each branch is a real way the program
gets started:

  1. `app.exe list > out.txt`   stdout is already a redirected file or pipe,
                                inherited from the caller. Do NOTHING: attaching
                                a console here would send the output to a window
                                instead of to the file the user asked for.
  2. `app.exe list` from cmd    no handles, but the parent has a console.
                                AttachConsole(-1) borrows it, so the output
                                lands in the window the user typed into.
  3. double-clicked with args   nobody's console to borrow. Allocate one, but
                                only when the caller says a window of our own is
                                better than silence.

The known wart in case 2: cmd.exe does not wait for a GUI-subsystem process, so
it prints its next prompt immediately and our output arrives underneath it. That
is cosmetic and unavoidable without shipping a second console-subsystem binary,
which costs more than it buys.
"""

from __future__ import annotations

import os
import sys

ATTACH_PARENT_PROCESS = -1


def usable(stream) -> bool:
    """Can this stream actually carry output to somewhere real?

    PyInstaller's windowed mode may leave None, and some versions substitute a
    stub that swallows writes without complaining -- which is worse, because
    printing appears to work and nothing comes out. A real stream is backed by a
    file descriptor; the stub is not, so that is what distinguishes them.
    """
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
        return stream.fileno() >= 0
    except Exception:
        return False


def _reopen() -> None:
    """Point the standard streams at the console we just acquired."""
    try:
        sys.stdout = open("CONOUT$", "w", buffering=1, errors="replace")
        sys.stderr = open("CONOUT$", "w", buffering=1, errors="replace")
    except OSError:
        return
    try:
        sys.stdin = open("CONIN$", "r")
    except OSError:
        # Nothing here reads from stdin; losing it is not worth failing over.
        pass


def ensure_output(*, allocate: bool = True) -> bool:
    """Make sure printing goes somewhere a person can see. True if it will.

    A no-op everywhere except a Windows build that has no console, which means
    this can be called unconditionally from the launcher without the launcher
    needing to know how it was packaged.
    """
    if os.name != "nt":
        return True
    if usable(sys.stdout):
        return True  # case 1: redirected, and redirection must be respected

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
    except Exception:
        return False

    if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):  # case 2
        _reopen()
        return usable(sys.stdout)
    if allocate and kernel32.AllocConsole():  # case 3
        _reopen()
        return usable(sys.stdout)
    return False
