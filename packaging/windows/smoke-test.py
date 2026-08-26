#!/usr/bin/env python3
"""Prove a built binary works, before anyone downloads it.

    python packaging\\windows\\smoke-test.py "dist\\WoW Addons from GitHub"

Runs against whatever build.py produced, so it can be used from a Linux
checkout against the Linux binary as a pre-flight and from CI against the real
.exe. The Windows-only assertions skip themselves elsewhere and say so, rather
than passing quietly.

The one that matters most is the redirected-output check. The binary is built
`--windowed`, which means it starts with no console; winconsole.py has to
notice when stdout is already a pipe and leave it alone. Get that wrong and
`app.exe list > out.txt` writes to a console window instead of the file --
which no test that only looks at exit codes would ever catch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WINDOWS = os.name == "nt"
APP_NAME = "WoW Addons from GitHub"


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def find_binary(folder: Path) -> Path:
    exe = folder / (f"{APP_NAME}.exe" if WINDOWS else APP_NAME)
    if not exe.is_file():
        fail(f"no binary at {exe}")
    return exe


def run(exe: Path, *args: str, env: dict | None = None, timeout: int = 120):
    """Always through a pipe, which is the redirected case winconsole must respect."""
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else Path("dist") / APP_NAME).resolve()
    exe = find_binary(folder)
    print(f"== Testing {exe}")

    print("== It prints to a redirected pipe rather than to a console of its own")
    result = run(exe, "--help")
    if result.returncode != 0:
        fail(f"--help exited {result.returncode}: {result.stderr[:400]}")
    if not result.stdout.strip():
        fail("--help produced no output on a pipe -- winconsole stole it back to a console")
    for command in ("init", "scan", "list", "set", "update", "where", "gui"):
        if command not in result.stdout:
            fail(f"--help does not mention '{command}'")

    print("== Its usage line names the program, not entry.py")
    first = result.stdout.splitlines()[0]
    if "entry" in first.lower() or "addons.py" in first:
        fail(f"usage line is wrong: {first}")

    print("== A bad command still reports itself properly")
    result = run(exe, "nonsense-command")
    if result.returncode == 0:
        fail("an unknown command should not exit 0")
    if not (result.stderr.strip() or result.stdout.strip()):
        fail("an unknown command said nothing at all -- stderr has nowhere to go")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        addons = tmp / "wow" / "Interface" / "AddOns" / "SomeAddon"
        addons.mkdir(parents=True)
        (addons / "SomeAddon.toc").write_text(
            "## Title: Some Addon\n## Version: 1.0\n"
            "## X-Website: https://github.com/someone/SomeAddon\n"
        )
        config = tmp / "config"
        config.mkdir()
        # APPDATA on Windows, XDG_CONFIG_HOME elsewhere -- the two branches of
        # core.default_config_dir, exercised through the real binary.
        env = {"APPDATA": str(config)} if WINDOWS else {"XDG_CONFIG_HOME": str(config)}

        print("== It works end to end against a scratch WoW folder")
        for args in (("init", str(tmp / "wow")), ("scan",)):
            result = run(exe, *args, env=env)
            if result.returncode != 0:
                fail(f"{args[0]} exited {result.returncode}: {result.stderr[:400]}")

        result = run(exe, "list", env=env)
        if "SomeAddon" not in result.stdout:
            fail(f"list did not show the scanned addon: {result.stdout[:400]}")
        if "suggested: github:someone/SomeAddon" not in result.stdout:
            fail("the .toc suggestion did not survive into the build")

        print("== The manifest went where this platform keeps settings")
        manifest = config / "wow-addons" / "manifest.json"
        if not manifest.is_file():
            fail(f"no manifest at {manifest}")
        written = json.loads(manifest.read_text())
        # The manifest holds several installs; the AddOns folder lives inside
        # one of them. Reading the old flat key here would have passed for the
        # wrong reason -- KeyError, not a missing folder.
        folders = [i.get("addons_dir") for i in written.get("installs", {}).values()]
        if not folders or None in folders:
            fail(f"the manifest records no AddOns folder: {written}")
        result = run(exe, "where", env=env)
        if str(config) not in result.stdout:
            fail(f"`where` does not point at {config}: {result.stdout[:400]}")

        print("== The window opens and stays open")
        # If Tk were missing or the GUI module had not been collected, this exits
        # within a second. Staying up is the pass condition.
        display = os.environ.get("DISPLAY")
        if not WINDOWS and not display:
            print("   (skipped: no display and not Windows)")
        else:
            process = subprocess.Popen(
                [str(exe)], env={**os.environ, **env},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                time.sleep(15)
                if process.poll() is not None:
                    out, err = process.communicate()
                    fail(f"the window exited on its own ({process.returncode}): {err[:400] or out[:400]}")
                print("   the window stayed up")
            finally:
                process.kill()
                process.wait(timeout=30)

    print()
    print(f"OK -- {exe.name} runs its CLI, keeps redirected output, and opens its window.")


if __name__ == "__main__":
    main()
