# Draft: a desktop UI, shipped as a Windows .exe and a Linux AppImage

**Status: draft for discussion. Nothing here is built yet.** The goal is that someone
picking this up can start at Milestone 1 without re-deciding anything settled below.

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

> **Verify before committing to this:** that the Linux AppImage base actually ships Tk. The
> `python-appimage` manylinux builds are believed to include `tkinter`, but confirm by running
> `python -c "import tkinter"` inside the chosen base image before Milestone 4. If it does
> not, either pick a base that does or bundle Tcl/Tk explicitly. On Windows this is a
> non-issue: python.org builds include Tk and PyInstaller collects it.

Use `ttk` widgets (`ttk.Treeview` for the table), not the ancient `tk.*` ones — same
stdlib, considerably less dated.

## 3. The refactor this needs first

`addons.py` is already close to the right shape: the engine functions (`scan_installed`,
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

## 5. Windows prerequisites

These are bugs today, independent of the UI, and block a credible Windows build:

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

   Removal differs too: a junction is removed with `os.rmdir`, not `os.unlink`, and
   `Path.is_symlink()` returns True for junctions on modern Python — verify the
   detach path on a real Windows box, not by reasoning.

2. **Manifest to `%APPDATA%`.** Currently lands in `C:\Users\<you>\.config\wow-addons\`.
   Should be `%APPDATA%\wow-addons\` on Windows, `$XDG_CONFIG_HOME` elsewhere.

Both belong in `core.py` and both want a test. CI already runs on `windows-latest`, so a
junction test will actually be exercised.

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

### Linux — AppImage

Two routes, in order of preference:

1. **`python-appimage`** — takes a `requirements.txt` and an entry point and produces an
   AppImage around a manylinux Python. Least work, and the project has no dependencies,
   which is the case it handles best.
2. **PyInstaller `--onedir` + `linuxdeploy` + `appimagetool`** — more control, more moving
   parts. Fall back to this if route 1 cannot supply Tk.

Build on the **oldest glibc you intend to support**, not on Debian 13 — an AppImage built
against a new glibc will not run on older distributions, and this is the single most common
way AppImages ship broken.

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

| # | Work | Rough effort |
|---|---|---|
| 1 | Split into `core` / `cli` / `gui` packages; extract `update_addon`; tests still green | 1 day |
| 2 | Windows junctions + `%APPDATA%`, with tests on the existing Windows CI | 0.5 day |
| 3 | Tkinter window: folder picker, addon table, Set-source dialog, threaded update with progress | 2–3 days |
| 4 | PyInstaller Windows build + release workflow | 0.5 day |
| 5 | AppImage build + release workflow | 1 day |
| 6 | Real-hardware testing both platforms; README rewrite for non-technical users | 1–2 days |

**≈ 6–8 focused days.** Milestones 1 and 2 are worth doing regardless of whether the UI
happens — one is a latent maintenance problem, the other is a real Windows bug.

## 9. Open questions

1. **Does the CLI stay supported?** This plan assumes yes, and that it costs almost nothing
   once `core` is extracted. Say if you would rather the UI simply replace it.
2. **Auto-update of the app itself?** Deliberately excluded — it is a large amount of work
   and, on Windows, roughly doubles the code-signing problem. Manual download for now.
3. **A "check for addon updates on launch" toggle?** Cheap to add, but it means the app
   makes network calls without the user asking. Given why this project exists, it should be
   **off by default** if it exists at all.
4. **Do you want a packaged CLI too**, or is the terminal path "install Python and run the
   script"? Affects whether Milestone 4 ships one binary or two.
5. **Minimum Linux version to support** — decides the AppImage build image.
