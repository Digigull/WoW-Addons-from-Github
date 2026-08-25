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
    """Point the standard streams at the console we just acquired.

    Windows only, and the guard is not a formality. CONOUT$ and CONIN$ are
    console devices there; anywhere else they are just filenames, so calling
    this off Windows silently creates two junk files in the working directory.
    One of them reached a commit that way, and since CONOUT$ is a reserved name
    on Windows, `git checkout` then refused the whole repository with
    "invalid path 'CONOUT$'" -- every Windows CI job died before running a
    single test.

    Only the ones that need it. A caller can perfectly well redirect stdout and
    leave stderr behind -- `app.exe list > out.txt` run by a parent that passes
    a pipe for one and nothing for the other -- and replacing the working half
    would throw away the redirection this whole module exists to respect.
    """
    if os.name != "nt":
        return
    for name in ("stdout", "stderr"):
        if not usable(getattr(sys, name, None)):
            try:
                setattr(sys, name, open("CONOUT$", "w", buffering=1, errors="replace"))
            except OSError:
                pass
    if not usable(getattr(sys, "stdin", None)):
        try:
            sys.stdin = open("CONIN$", "r")
        except OSError:
            # Nothing here reads from stdin; losing it is not worth failing over.
            pass


def _silence() -> None:
    """Last resort: point whatever is still unusable at the null device.

    Reached when no console could be had at all -- a service, a scheduled task,
    a CI runner. Writing into nothing is not good, but the alternative is that
    the first `print` raises `AttributeError: 'NoneType' object has no attribute
    'write'`, and a crash nobody can see is strictly worse than a message nobody
    can see: the crash also loses the exit code that says whether the run
    worked.
    """
    for name in ("stdout", "stderr"):
        if not usable(getattr(sys, name, None)):
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except OSError:
                pass


def ensure_output(*, allocate: bool = True) -> bool:
    """Make sure printing goes somewhere. True if it goes somewhere VISIBLE.

    A no-op everywhere except a Windows build that has no console, which means
    this can be called unconditionally from the launcher without the launcher
    needing to know how it was packaged.

    Both streams are checked, not just stdout: every warning and every failure
    message in the CLI goes to stderr, so a stderr left as None turns the first
    warning into `AttributeError: 'NoneType' object has no attribute 'write'`.

    A False return means no console could be obtained -- but the streams are
    still left safe to write to, so callers do not have to check. Windows CI is
    a real example of that environment: the runner's shell has no console to
    attach to and no session to allocate one in.
    """
    if os.name != "nt":
        return True
    if usable(sys.stdout) and usable(sys.stderr):
        return True  # case 1: redirected, and redirection must be respected

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
    except Exception:
        # No way to ask for a console at all. Same outcome as asking and being
        # refused, so take the same fallback rather than returning with the
        # streams still unwritable.
        _silence()
        return False

    if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):  # case 2
        _reopen()
        return usable(sys.stdout) and usable(sys.stderr)
    if allocate and kernel32.AllocConsole():  # case 3
        _reopen()
        return usable(sys.stdout) and usable(sys.stderr)

    _silence()  # nowhere to show it; at least do not crash trying
    return False
