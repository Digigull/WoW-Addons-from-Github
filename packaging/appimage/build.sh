#!/usr/bin/env bash
#
# Build the Linux AppImage.
#
#     packaging/appimage/build.sh              build for the running architecture
#     PYTHON_VERSION=3.12 packaging/.../build.sh
#
# Produces dist/WoW-Addons-from-GitHub-<arch>.AppImage.
#
# Route: python-appimage, which wraps a manylinux Python that already contains
# Tkinter and relocates the matching Tcl/Tk into the image. That choice is what
# decides the glibc floor, and it is the reason this does NOT need to run on an
# ancient distribution: the bundled interpreter is built against manylinux2014
# (glibc 2.17, ~2012), whatever the machine doing the building is running. The
# usual way to ship a broken AppImage -- build on something new, link against a
# glibc nobody has -- does not apply here, because nothing of ours is compiled.
#
# python-appimage is a BUILD-time dependency only. Nothing lands in the shipped
# image except the standard library and the wowaddons package.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
RECIPE="$HERE/WoW-Addons-from-GitHub"

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PYTHON_APPIMAGE_VERSION="${PYTHON_APPIMAGE_VERSION:-1.4.6}"
ARCH="$(uname -m)"
OUT="$ROOT/dist"

# appimagetool is itself an AppImage, so building one normally needs FUSE. CI
# runners and most containers do not have it; extract-and-run costs a little
# time and removes the requirement entirely. It affects the BUILD only -- what
# the finished AppImage needs at runtime is unchanged.
export APPIMAGE_EXTRACT_AND_RUN=1

# `local+wowaddons` in requirements.txt is resolved with importlib against the
# build interpreter, so the package has to be importable from here.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "== Building for Python $PYTHON_VERSION on $ARCH"

# Build tooling goes in its own virtualenv rather than into the user's Python.
# Recent distributions refuse a plain `pip install` outside one (PEP 668), and
# even where they allow it, a build script has no business changing the
# environment of the machine it runs on. Set BUILD_PYTHON to reuse an
# interpreter that already has python-appimage instead.
if [ -n "${BUILD_PYTHON:-}" ]; then
    PY="$BUILD_PYTHON"
else
    VENV="$ROOT/.build-venv"
    if [ ! -x "$VENV/bin/python" ]; then
        echo "== Creating $VENV for the build tooling"
        python3 -m venv "$VENV"
    fi
    PY="$VENV/bin/python"
    if ! "$PY" -c "import python_appimage" 2>/dev/null; then
        echo "== Installing python-appimage $PYTHON_APPIMAGE_VERSION (build-time only)"
        "$PY" -m pip install --quiet --upgrade pip
        "$PY" -m pip install --quiet "python-appimage==$PYTHON_APPIMAGE_VERSION"
    fi
fi

# Regenerate the icon so it can never drift from the script that draws it.
python3 "$HERE/make_icon.py"

# `local+` copies the package tree verbatim, so a developer's stale bytecode
# would be copied in with it. Cheap to avoid, and it keeps builds identical
# whatever state the working copy is in.
find "$ROOT/wowaddons" -name '__pycache__' -type d -prune -exec rm -rf {} +

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

( cd "$WORK" && "$PY" -m python_appimage build app \
        --python-version "$PYTHON_VERSION" \
        "$RECIPE" )

# The output is named from the desktop file's Name=, which has spaces in it
# because that is what belongs in a menu. A download does not want them.
BUILT="$(find "$WORK" -maxdepth 1 -name '*.AppImage' -print -quit)"
if [ -z "$BUILT" ]; then
    echo "build produced no AppImage" >&2
    exit 1
fi

mkdir -p "$OUT"
FINAL="$OUT/WoW-Addons-from-GitHub-$ARCH.AppImage"
mv "$BUILT" "$FINAL"
chmod +x "$FINAL"

echo "== Built $FINAL ($(du -h "$FINAL" | cut -f1))"
