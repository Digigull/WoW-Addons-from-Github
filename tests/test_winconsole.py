#!/usr/bin/env python3
"""Tests for the console a windowed Windows build has to grow on demand.

The Windows binary is built `--windowed`, so it starts with no console and
`sys.stdout` is None. Every command-line path has to acquire one before it
prints, starting with argparse handling --help.

The failure worth guarding is silent in the worst way: get the "already
redirected" case wrong and `app.exe list > out.txt` writes to a console window
instead of the file, leaving the file empty. No test that checks exit codes
would notice, and neither would anyone running it interactively.

Most of this is Windows-only at runtime, but `usable()` decides the whole
question and is ordinary Python, so it is tested everywhere.
"""

import io
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wowaddons import winconsole  # noqa: E402


class Discarded(io.IOBase):
    """What PyInstaller's windowed mode can leave behind: writes vanish quietly.

    This is the dangerous one. It accepts everything and delivers nothing, so a
    check as simple as `sys.stdout is not None` would decide there is nothing
    to do and every message would be lost.
    """

    def write(self, text):
        return len(text)

    def flush(self):
        pass


class Detached(io.IOBase):
    """A stream whose fileno() raises, which is what a detached stub does."""

    def write(self, text):
        return len(text)

    def flush(self):
        pass

    def fileno(self):
        raise io.UnsupportedOperation("no file descriptor")


class WhatCountsAsSomewhereToWrite(unittest.TestCase):
    def test_a_real_stream_counts(self):
        self.assertTrue(winconsole.usable(sys.__stdout__))

    def test_a_real_file_counts(self):
        # This is the redirected case -- `app.exe list > out.txt` -- and it is
        # the one that must be left alone.
        import tempfile

        with tempfile.TemporaryFile("w") as handle:
            self.assertTrue(winconsole.usable(handle))

    def test_none_does_not(self):
        self.assertFalse(winconsole.usable(None))

    def test_a_stream_that_swallows_writes_does_not(self):
        self.assertFalse(winconsole.usable(Discarded()))

    def test_a_stream_with_no_file_descriptor_does_not(self):
        self.assertFalse(winconsole.usable(Detached()))

    def test_an_in_memory_buffer_does_not(self):
        # StringIO has no descriptor, so it reads as "nowhere real" -- correct
        # for this question, since nobody outside the process can see it.
        self.assertFalse(winconsole.usable(io.StringIO()))

    def test_a_stream_that_raises_on_write_does_not(self):
        class Broken(io.IOBase):
            def write(self, text):
                raise OSError("closed")

        self.assertFalse(winconsole.usable(Broken()))


class AcquiringOne(unittest.TestCase):
    def tearDown(self):
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    @unittest.skipIf(os.name == "nt", "off-Windows behaviour")
    def test_it_is_a_no_op_off_windows(self):
        # Linux and macOS builds always have real streams; there is nothing to
        # attach to and nothing to do.
        self.assertTrue(winconsole.ensure_output())

    def test_redirected_output_is_left_alone(self):
        # The important one: it must not swap a redirected stdout for a console.
        import tempfile

        with tempfile.TemporaryFile("w") as handle:
            sys.stdout = handle
            self.assertTrue(winconsole.ensure_output())
            self.assertIs(sys.stdout, handle, "redirection was overridden")

    def test_a_missing_stderr_is_not_treated_as_fine(self):
        """Every warning and failure message in the CLI goes to stderr.

        A stderr left as None turns the first warning into an AttributeError, so
        "stdout works" cannot be the whole answer.

        What is asserted is the contract, not an outcome: whether a console can
        actually be obtained depends on where the process was started, and on a
        CI runner the answer is no. So -- if it claims success, stderr really
        works; either way a working stdout is left alone and stderr is safe to
        write to.
        """
        import tempfile

        if os.name != "nt":
            # Off Windows the streams are always real, so a None stderr is a
            # state that cannot arise and ensure_output is a documented no-op.
            self.assertTrue(winconsole.ensure_output())
            return

        with tempfile.TemporaryFile("w") as handle:
            sys.stdout, sys.stderr = handle, None
            try:
                if winconsole.ensure_output():
                    self.assertTrue(winconsole.usable(sys.stderr),
                                    "it reported success with stderr still broken")
                self.assertIs(sys.stdout, handle, "a working stdout was replaced")
                print("", file=sys.stderr)  # must not raise, whatever happened
            finally:
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    def test_giving_up_still_leaves_the_streams_writable(self):
        """False from ensure_output must not mean "the next print crashes".

        Windows CI is the real example: no console to attach to and no session
        to allocate one in. A crash nobody can see is worse than a message
        nobody can see -- the crash also loses the exit code.
        """
        if os.name != "nt":
            self.skipTest("_silence is only reached on the Windows path")
        sys.stdout = sys.stderr = None
        try:
            winconsole.ensure_output()
            print("this must not raise")
            print("nor this", file=sys.stderr)
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    def test_the_null_fallback_makes_broken_streams_writable(self):
        # _silence itself is platform-independent, so its behaviour is checked
        # everywhere even though only Windows can reach it.
        sys.stdout = sys.stderr = None
        try:
            winconsole._silence()
            self.assertTrue(winconsole.usable(sys.stdout))
            self.assertTrue(winconsole.usable(sys.stderr))
            print("writable", file=sys.stderr)
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    def test_the_null_fallback_leaves_a_working_stream_alone(self):
        import tempfile

        with tempfile.TemporaryFile("w") as handle:
            sys.stdout, sys.stderr = handle, None
            try:
                winconsole._silence()
                self.assertIs(sys.stdout, handle)
                self.assertTrue(winconsole.usable(sys.stderr))
            finally:
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    def test_reopening_keeps_a_stream_that_already_works(self):
        # _reopen must replace only what is broken. Clobbering a working
        # redirected stdout would defeat the point of the whole module.
        import tempfile

        with tempfile.TemporaryFile("w") as handle:
            sys.stdout, sys.stderr = handle, None
            try:
                winconsole._reopen()
                self.assertIs(sys.stdout, handle, "a working stdout was replaced")
            finally:
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    def test_reopening_writes_nothing_to_disk_off_windows(self):
        """CONOUT$ is a console device on Windows and a filename everywhere else.

        Called off Windows, _reopen used to create CONOUT$ and CONIN$ in the
        working directory. One of them was committed that way -- and because
        the name is reserved on Windows, `git checkout` then refused the entire
        repository with "invalid path 'CONOUT$'", so every Windows CI job died
        before running a single test.
        """
        import tempfile

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                sys.stdout = sys.stderr = None
                winconsole._reopen()
            finally:
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
                leftovers = sorted(os.listdir(tmp))
                os.chdir(cwd)
        if os.name == "nt":
            self.assertEqual(leftovers, [], "console devices are not files on Windows")
        else:
            self.assertEqual(leftovers, [], "_reopen created files off Windows")

    @unittest.skipUnless(os.name == "nt", "Windows-only path")
    def test_a_windows_process_with_a_console_needs_nothing(self):
        # The test runner itself has one, so this exercises the early return
        # against a real console rather than a simulated one.
        self.assertTrue(winconsole.ensure_output())


class TheLauncherUsesIt(unittest.TestCase):
    def test_output_is_secured_before_argparse_runs(self):
        # --help writes to stdout. If the console is acquired after parsing,
        # the windowed build crashes on the very first thing a user tries.
        import ast

        source = (pathlib.Path(winconsole.__file__).parent / "__main__.py").read_text()
        body = ast.parse(source)
        launcher = next(
            n for n in ast.walk(body) if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        # By line number, not by walk order: ast.walk is breadth-first and says
        # nothing about what runs first, which is the whole question here.
        lines = {}
        for node in ast.walk(launcher):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                lines.setdefault(node.func.attr, []).append(node.lineno)

        self.assertIn("ensure_output", lines, "the launcher never acquires a console")
        self.assertIn("main", lines, "the launcher never hands over to the CLI")
        self.assertLess(
            min(lines["ensure_output"]), min(lines["main"]),
            "the console has to exist before the CLI writes to it",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
