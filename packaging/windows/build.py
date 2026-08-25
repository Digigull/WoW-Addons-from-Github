#!/usr/bin/env python3
"""Build the Windows .exe.

    python packaging\\windows\\build.py

Produces dist\\WoW-Addons-from-GitHub-windows-x64.zip, holding a folder you
unzip anywhere and run. Written in Python rather than PowerShell so that it is
readable, quotes its own arguments, and can be reasoned about from a Linux
checkout -- which is where most of this project is edited.

TWO CHOICES WORTH KNOWING ABOUT

--onedir, not --onefile. A one-file build is a self-extracting archive, which
is also what a great deal of malware looks like, so heuristic antivirus flags
it far more often. It also unpacks to a temporary directory on every launch,
which is slower. A zipped folder is a worse download and a better program.

--windowed. Double-clicking must open a window, not a black console box. The
cost is that the process starts with no stdout at all, so every command-line
path has to acquire a console first; wowaddons/winconsole.py does that, and is
the reason one binary can be both the app and the CLI.

PyInstaller is a BUILD-time dependency. Nothing of it is imported by the tool,
and the shipped program is still standard library only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

APP_NAME = "WoW Addons from GitHub"  # what Windows shows: Start menu, taskbar, title bar
ZIP_STEM = "WoW-Addons-from-GitHub-windows-x64"  # what people download: no spaces
PYINSTALLER_VERSION = os.environ.get("PYINSTALLER_VERSION", "6.11.1")


def run(command: list[str]) -> None:
    print("==", " ".join(command))
    subprocess.run(command, check=True)


def build() -> Path:
    dist = ROOT / "dist"
    work = ROOT / "build"
    for stale in (dist / APP_NAME, work):
        if stale.exists():
            shutil.rmtree(stale)

    # Regenerate the icon so it can never drift from the script that draws it.
    run([sys.executable, str(ROOT / "packaging" / "make_icon.py")])

    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", APP_NAME,
        "--icon", str(HERE / "wow-addons-from-github.ico"),
        # wowaddons.gui is imported inside a function, in a try/except, so that
        # a missing Tk degrades to a message rather than a traceback. That is
        # good for a checkout and invisible to a static analyser, so name it --
        # without this the build succeeds and the .exe has no window.
        "--hidden-import", "wowaddons.gui",
        "--hidden-import", "tkinter",
        "--paths", str(ROOT),
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        str(HERE / "entry.py"),
    ])

    built = dist / APP_NAME
    # PyInstaller only ever builds for the platform it runs on, so this script
    # produces the shipped .exe on Windows and a Linux binary anywhere else.
    # That is worth keeping working rather than rejecting: running it from a
    # Linux checkout proves the entry point resolves and that wowaddons.gui and
    # Tk were actually collected, which are the failures worth catching before
    # a Windows runner spends four minutes finding them.
    exe = built / (f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME)
    if not exe.is_file():
        raise SystemExit(f"PyInstaller produced no {exe}")
    return built


def zip_up(folder: Path) -> Path:
    archive = folder.parent / f"{ZIP_STEM}.zip"
    archive.unlink(missing_ok=True)
    # Everything goes under one top-level directory, so unzipping into Downloads
    # does not strew a hundred files across it.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                out.write(path, Path(ZIP_STEM) / path.relative_to(folder))
    return archive


if __name__ == "__main__":
    if not shutil.which(sys.executable):
        raise SystemExit("no interpreter?")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "--quiet",
             f"pyinstaller=={PYINSTALLER_VERSION}"])

    folder = build()
    if os.name != "nt":
        print(f"== Built {folder} -- a Linux binary, because that is this machine.")
        print("   The shipped .exe comes from running this on Windows; this run only")
        print("   proves the entry point, the hidden imports and Tk collection.")
        raise SystemExit(0)

    archive = zip_up(folder)
    size = archive.stat().st_size / 1024 / 1024
    print(f"== Built {archive} ({size:.0f} MB)")
