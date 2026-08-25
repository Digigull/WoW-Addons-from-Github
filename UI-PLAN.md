# A desktop UI, shipped as a Windows .exe and a Linux AppImage

**Status: Milestones 1, 2, 3 and 5 are done. The Linux AppImage is built, and the two
Windows bugs that gate a credible `.exe` are fixed. Milestone 4 — PyInstaller and a
Windows release job — is what remains.** Everything below is kept as written except where
a decision has since been settled by contact with reality; those places say so.

The target: a user downloads one file, double-clicks it, points it at their WoW folder,
sees their addons in a list, picks a source for each, and clicks Update. No Python, no
terminal, no install.

## What this is not

- Not a catalogue or a store. You supply the repositories; there is no curated list.
- Not an account system. Nothing to sign into.
- Not telemetry. This stays true of the UI: it contacts the hosts in your own manifest
  and nothing else. That property is the reason the project exists and should be treated
  as a constraint, not a preference.
- Not a replacement for the CLI. The CLI stays, and stays first-class — the UI is a second
  front end over the same engine.

## 1. The window

One window is enough. Sketch:

```
┌─ WoW Addons from GitHub ─────────────────────────────────────────────┐
│ WoW folder:  ~/Games/Ascension/Interface/AddOns        [ Change… ]   │
├──────────────────────────────────────────────────────────────────────┤
│ Addon                  Source                    Installed   Status  │
│ ─────────────────────────────────────────────────────────────────────│
│ GnomeWorks             local: ~/repo/GnomeWorks  linked      ok      │
│ PasslootBiS            local: ~/repo/PasslootBiS linked      ok      │
│ SomeAddon              github:someone/SomeAddon  v1.0.2      v1.1 ►  │
│ OldThing               (unmanaged)               0.3         —       │
│ Suggested              (unmanaged)               1.4         suggests│
│                                                              github: │
│                                                              a/b     │
├──────────────────────────────────────────────────────────────────────┤
│ [ Rescan ]  [ Set source… ]  [ Update selected ]  [ Update all ]     │
│ Ready.                                          [████████░░░░] 8/12  │
└──────────────────────────────────────────────────────────────────────┘
```

**Set source…** opens a small modal over the selected addon:

```
┌─ Source for "SomeAddon" ──────────────────────┐
│  ( ) Local folder   [____________] [Browse…]  │
│      [ ] copy files instead of linking        │
│  (•) GitHub repo    [owner/repo____________]  │
│      [ ] track branch: [______]               │
│  ( ) Leave unmanaged                          │
│                            [ Cancel ] [ Save ]│
└───────────────────────────────────────────────┘
```

Behaviours worth stating, because they are where a UI usually gets this wrong:

- **Suggestions are shown, never applied.** `scan` guesses a source from `.toc` headers. In
  the list that appears as a hint in the Status column with an "Accept" affordance. It must
  never silently become the addon's source — a header is the author's claim about where the
  code lives, not the user's decision to install from there.
- **Nothing is destructive without saying so.** Binding an addon that exists as real files
  moves the old folder to `<Name>.replaced`. The UI should say that in the confirm step,
  because in a CLI the user reads the log and in a GUI they will not.
- **Per-row failures stay on their row.** One unreachable repo marks that row failed and
  leaves the rest updated. Do not raise a modal error dialog per failure.
- **A first run with no WoW folder set** opens straight into the folder picker rather than
  an empty list.

## 2. Toolkit: Tkinter

**Recommendation: Tkinter**, from the standard library.

| | Tkinter | PySide6 / Qt | Local web UI |
|---|---|---|---|
| New dependencies | none | large | none |
| Bundled size | ~15 MB | ~150–200 MB | ~12 MB |
| Licence considerations | none | LGPL relinking obligations on a public binary | none |
| Looks | dated but fine | modern | modern |
| Fits this UI? | yes — a table, four buttons, one modal | overkill | yes, but it is a browser tab, not an app |

The UI needed here is a table, four buttons and one dialog. Qt buys polish this does not
need and costs a 10× binary plus licence obligations on a public download. A local web UI
is genuinely tempting and stays stdlib, but "double-click and a browser tab opens" is a
worse answer to "I want an app" than a real window, and it invites a Windows firewall prompt.

Tkinter keeps the project's defining property — **stdlib only, nothing to install** — intact
all the way to the UI.

> **Settled: the base does ship Tk.** The `python-appimage` manylinux builds include
> `_tkinter`, and the tool relocates the matching Tcl/Tk into `usr/share/tcltk` and points
> `TCL_LIBRARY`, `TK_LIBRARY` and `TKPATH` at it. Route 1 stands; no fallback was needed.
>
> One trap found on the way, which the recipe and `tests/test_packaging.py` now guard:
> those exports are set by the wrapper script at `usr/bin/pythonX.Y`, **not** by the
> interpreter under `opt/`, and a recipe's own entry point *replaces* the base `AppRun`.
> An entry point that calls the interpreter directly yields an AppImage that starts, prints
> help, scans a folder, passes every CLI check — and cannot open a window. Use
> `{{ python-executable }}`, which is the wrapper.
>
> `packaging/appimage/smoke-test.sh` re-proves this on every build rather than trusting it:
> it opens a real Tk window inside the image, and then launches the AppImage itself under
> Xvfb and fails if it exits on its own.

Use `ttk` widgets (`ttk.Treeview` for the table), not the ancient `tk.*` ones — same
stdlib, considerably less dated.

## 3. The refactor this needs first — **done**

`addons.py` was already close to the right shape: the engine functions (`scan_installed`,
`latest_github`, `install_zip`, `install_local`, manifest `load`/`save`) have no opinion
about the terminal. The `cmd_*` functions are where presentation and orchestration are
tangled — `cmd_update` loops, decides, installs, prints and saves, all in one body.

A GUI cannot reuse `cmd_update`, and duplicating its logic would guarantee the two front
ends drift. So:

```
wowaddons/
    core.py      engine: manifest, scanning, sources, install. No printing, ever.
    cli.py       argparse + the current step/note/warn output
    gui.py       Tkinter
addons.py        thin launcher: GUI when double-clicked, CLI when given arguments
tests/
```

The key extraction is one function:

```python
def update_addon(entry, root, *, force=False, dry_run=False, progress=None) -> Result:
    """Update a single addon. Returns what happened; never prints.

    progress: optional callable taking (stage, detail) so a front end can show
    "checking…", "downloading…", "installing…" without core knowing what a
    terminal or a progress bar is.
    """
```

The CLI's per-addon loop and the GUI's worker thread then both call it, and the
"one failure does not sink the run" rule lives in one place instead of two.

**Keep `Fail` as the error type.** It already carries its message without printing, which is
exactly what a GUI needs in order to put that message in a row rather than on stdout.

**Keep the tests passing throughout.** They import `addons.py` by path today; point them at
`wowaddons.core` and they should otherwise survive the move unchanged. If a test needs
rewriting to accommodate the refactor, that is a signal the refactor changed behaviour.

## 4. Threading

Every network call must be off the UI thread or the window freezes mid-download, which on
Windows means "not responding" and a user force-quitting a half-finished install.

Standard Tk pattern, no new dependencies:

- Work runs on a `threading.Thread`.
- The worker pushes `(kind, payload)` tuples onto a `queue.Queue`.
- The UI drains that queue from a `root.after(100, drain)` poll and touches widgets only there.
- **Never touch a widget from the worker thread.** Tk is not thread-safe, and violations
  usually appear as intermittent crashes rather than obvious errors.

Cancellation: a `threading.Event` the worker checks between addons. Mid-download
cancellation is not worth the complexity — cancelling between addons is enough, and it
leaves the manifest consistent.

## 5. Windows prerequisites — **done**

These were bugs independent of the UI, and blocked a credible Windows build:

1. **Junctions instead of symlinks.** `os.symlink` on Windows requires administrator rights
   or Developer Mode. Directory junctions require neither and WoW reads them identically.
   `_winapi.CreateJunction(src, dst)` exists in CPython but is private; `subprocess` calling
   `mklink /J` is the boring, documented alternative. Roughly:

   ```python
   if os.name == "nt":
       subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True)
   else:
       dst.symlink_to(src, target_is_directory=True)
   ```

   Removal differs too: a junction is removed with `os.rmdir`, not `os.unlink`.
   `Path.is_symlink()`'s treatment of junctions has not been consistent across Python
   versions, so `core.is_link()` checks the reparse tag rather than trusting it. That
   check is the load-bearing one: everything that replaces an installed addon asks "is
   it a link?" first and calls `shutil.rmtree` if the answer is no, so an `is_link()`
   that missed a junction would send `rmtree` **through** it into the user's own source
   checkout. There is a test for exactly that, and it runs on the Windows CI.

2. **Manifest to `%APPDATA%`.** Used to land in `C:\Users\<you>\.config\wow-addons\`.
   Now `%APPDATA%\wow-addons\` on Windows, `$XDG_CONFIG_HOME` elsewhere — and the old
   location is still *read* when the new one is empty, so upgrading does not look like
   "you have no addons bound". The next write completes the move on its own.

Both landed in `core.py` with tests, and because the CI matrix includes `windows-latest`
the junction paths are actually executed there rather than reasoned about.

## 6. Packaging

### Windows — PyInstaller

```
pyinstaller --noconfirm --windowed --name "WoW Addons from GitHub" addons.py
```

- `--windowed` suppresses the console window for the GUI. **The CLI still needs a console**,
  so either ship two binaries or (better) build windowed and have the launcher allocate a
  console when it detects command-line arguments.
- Prefer **`--onedir` in a zip over `--onefile`.** One-file binaries are self-extracting
  archives, which is also what a lot of malware looks like, and they draw heuristic
  antivirus flags far more often. One-dir also starts faster.

### Linux — AppImage — **done, route 1**

Two routes, in order of preference:

1. **`python-appimage`** — takes a `requirements.txt` and an entry point and produces an
   AppImage around a manylinux Python. Least work, and the project has no dependencies,
   which is the case it handles best.
2. **PyInstaller `--onedir` + `linuxdeploy` + `appimagetool`** — more control, more moving
   parts. Fall back to this if route 1 cannot supply Tk.

~~Build on the **oldest glibc you intend to support**~~ — **this turned out not to apply.**
The advice is right for anything compiled, and wrong here: with route 1 the glibc floor
comes from the bundled manylinux2014 interpreter (glibc 2.17, 2012) whatever the build
machine runs, because nothing in this project is compiled. CI builds on `ubuntu-22.04` for
the sake of the surrounding tooling, not for the floor.

> **Flag for the user:** AppImages need FUSE 2 at runtime, which recent Debian and Ubuntu
> releases no longer install by default. Users may need `libfuse2` (`libfuse2t64` on newer
> Debian), or must run with `--appimage-extract-and-run`. Document this in the README
> rather than letting people hit a bare "dlopen(): error loading libfuse.so.2".

### CI

Extend `.github/workflows/` with a release workflow triggered on tag push:

- `windows-latest` → build, zip, upload
- `ubuntu-latest` (oldest practical image) → build AppImage, upload
- attach both to the GitHub Release

The existing `tests.yml` should stay a separate, fast, every-push workflow. Do not merge
the two — a slow release build on every commit is how people stop watching CI.

## 7. Distribution reality

Worth deciding before shipping, because none of it is code:

- **SmartScreen.** An unsigned `.exe` shows "Windows protected your PC" and needs
  *More info → Run anyway*. It does not go away with downloads or time on its own.
- **Code signing** is the only real fix and is the largest cost: an OV certificate runs
  roughly $200–400/year and since 2023 requires a hardware token or cloud HSM. Azure Trusted
  Signing is far cheaper (~$10/month) but has identity-verification requirements that are
  worth checking against your situation *before* planning around it.
- **Antivirus false positives** on PyInstaller output are common. `--onedir` reduces them;
  signing reduces them further.
- **AppImage has none of these problems** — no signing expectation, no SmartScreen. If you
  want to see whether anyone uses this before spending money, the Linux build is the honest
  first release.

Suggested sequence: ship unsigned, document the click-through, and only buy a certificate
once there are enough Windows users for it to be worth it.

## 8. Milestones

| # | Work | Status |
|---|---|---|
| 1 | Split into `core` / `cli` / `gui` packages; extract `update_addon`; tests still green | **done** |
| 2 | Windows junctions + `%APPDATA%`, with tests on the existing Windows CI | **done** |
| 3 | Tkinter window: folder picker, addon table, Set-source dialog, threaded update with progress | **done** |
| 4 | PyInstaller Windows build + release workflow | next |
| 5 | AppImage build + release workflow | **done** |
| 6 | Real-hardware testing both platforms; README rewrite for non-technical users | Linux README done; hardware testing outstanding |

Milestone 2 was the gate on the Windows `.exe` and is done: a packaged build that needed
administrator rights to bind a `local:` source, or that hid its manifest under
`~/.config`, would not have been worth shipping.

**Still untested on real hardware.** The AppImage is verified by CI — it starts, opens a
window under Xvfb, and scans a folder — but nobody has yet run it on a desktop against an
actual WoW install. That is the next thing worth doing after Milestone 2, and it is the
kind of thing CI cannot stand in for.

## 9. Open questions

1. ~~**Does the CLI stay supported?**~~ **Yes, and it did cost almost nothing.** The CLI is
   unchanged in behaviour, `addons.py` still works from a checkout, and the AppImage runs
   the CLI too when given arguments.
2. **Auto-update of the app itself?** Deliberately excluded — it is a large amount of work
   and, on Windows, roughly doubles the code-signing problem. Manual download for now.
3. **A "check for addon updates on launch" toggle?** Cheap to add, but it means the app
   makes network calls without the user asking. Given why this project exists, it should be
   **off by default** if it exists at all.
4. **Do you want a packaged CLI too**, or is the terminal path "install Python and run the
   script"? Affects whether Milestone 4 ships one binary or two.
5. ~~**Minimum Linux version to support**~~ — **answered by the base image, not by a
   decision:** glibc 2.17 and newer, which is CentOS 7 era and older than anything anyone
   is realistically running.
