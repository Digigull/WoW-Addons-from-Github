#!/usr/bin/env bash
#
# Prove a built AppImage actually works, before anyone downloads it.
#
#     packaging/appimage/smoke-test.sh dist/WoW-Addons-from-GitHub-x86_64.AppImage
#
# The Tk checks are the point of this script. Whether the AppImage base ships a
# usable Tk was the one open question able to sink this whole approach, and it
# has to be re-proved on every build rather than assumed: a base that quietly
# dropped Tk would still produce an AppImage that starts, prints help, scans a
# folder, passes every test that does not open a window -- and then fails at the
# one thing people downloaded it for.
#
# Note that the AppImage's entry point is `python -m wowaddons`, not a bare
# interpreter, so the interpreter-level checks go through an extracted copy
# instead. That is the same Tcl/Tk-aware wrapper the app itself runs through.

set -euo pipefail

APP="${1:?usage: smoke-test.sh <path to .AppImage>}"
APP="$(cd -- "$(dirname -- "$APP")" && pwd)/$(basename -- "$APP")"
chmod +x "$APP"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAILED: $*" >&2; exit 1; }

# No FUSE in CI or in most containers. This is also how a user without libfuse2
# runs it, so exercising this path is exercising a real one.
RUN=("$APP")
if ! "$APP" --help >/dev/null 2>&1; then
    echo "== No FUSE here; using --appimage-extract-and-run"
    RUN=("$APP" --appimage-extract-and-run)
fi

echo "== The CLI starts and lists its commands"
"${RUN[@]}" --help > "$WORK/help.txt" || fail "--help did not run"
for command in init scan list set update where gui; do
    grep -q "  *$command " "$WORK/help.txt" || fail "--help does not mention '$command'"
done

echo "== Its usage line names the AppImage, not addons.py"
grep -qi "AppImage" "$WORK/help.txt" || fail "usage line does not name the AppImage: $(head -1 "$WORK/help.txt")"

echo "== It works end to end against a scratch WoW folder"
mkdir -p "$WORK/wow/Interface/AddOns/SomeAddon" "$WORK/config"
printf '## Title: Some Addon\n## Version: 1.0\n## X-Website: https://github.com/someone/SomeAddon\n' \
    > "$WORK/wow/Interface/AddOns/SomeAddon/SomeAddon.toc"
export XDG_CONFIG_HOME="$WORK/config"
"${RUN[@]}" init "$WORK/wow" >/dev/null || fail "init"
"${RUN[@]}" scan >/dev/null || fail "scan"
"${RUN[@]}" list > "$WORK/list.txt" || fail "list"
grep -q SomeAddon "$WORK/list.txt" || fail "list did not show the scanned addon"
grep -q "suggested: github:someone/SomeAddon" "$WORK/list.txt" \
    || fail "the .toc suggestion did not survive into the AppImage"

echo "== Unpacking to check the bundled interpreter"
( cd "$WORK" && "$APP" --appimage-extract >/dev/null ) || fail "could not extract the AppImage"
PY="$(find "$WORK/squashfs-root/usr/bin" -maxdepth 1 -name 'python3.*' -not -name '*-config' -print -quit)"
[ -n "$PY" ] || fail "no python wrapper in usr/bin"

echo "== The wowaddons package is inside, and imports"
"$PY" -c "import wowaddons.core, wowaddons.cli, wowaddons.gui; print('   modules ok')" \
    || fail "wowaddons is not importable inside the AppImage"

echo "== Tcl/Tk is bundled and a real window can be created"
cat > "$WORK/tkcheck.py" <<'PY'
import sys, tkinter, tkinter.ttk as ttk
root = tkinter.Tk()               # fails unless Tcl/Tk itself was found, not just imported
ttk.Treeview(root)                # the widget the addon table is built from
ttk.Style().theme_use("clam")     # the theme the window asks for
root.update_idletasks()
root.destroy()
print("   tk", tkinter.TkVersion, "on python", sys.version.split()[0])
PY
if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$PY" "$WORK/tkcheck.py" || fail "Tkinter cannot open a window inside the AppImage"
else
    echo "   (no xvfb-run: checking the import and the Tcl/Tk files only)"
    "$PY" -c "import tkinter; print('   tkinter importable')" || fail "Tkinter is missing"
    ls -d "$WORK/squashfs-root/usr/share/tcltk/tk"* >/dev/null 2>&1 \
        || fail "no Tcl/Tk library directory bundled -- the window would fail at runtime"
fi

echo "== The window itself actually opens"
if command -v xvfb-run >/dev/null 2>&1; then
    # No arguments means the GUI. If Tk were broken this exits within a second;
    # staying up until the timeout kills it is the pass condition, so 124 is the
    # result we want and a clean early exit is the failure.
    set +e
    xvfb-run -a timeout 20 "${RUN[@]}" >"$WORK/gui.log" 2>&1
    code=$?
    set -e
    [ "$code" -eq 124 ] || fail "the window exited on its own (code $code): $(tail -3 "$WORK/gui.log")"
    echo "   the window stayed up"
fi

echo
echo "OK -- $(basename "$APP") starts, has a working Tk, opens its window, and scans a folder."
