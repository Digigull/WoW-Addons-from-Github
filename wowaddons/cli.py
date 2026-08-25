"""The terminal front end: argparse, and the only place in the package that prints.

Same output shape as the converter's deploy script, so output from the two
reads the same way when you have both scrolling past.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, core
from .core import Fail

EPILOG = """\
    addons.py init ~/Games/Ascension
    addons.py scan
    addons.py set GnomeWorks local:.
    addons.py set SomeAddon github:owner/repo
    addons.py set OneOfMany github:owner/repo#OneOfMany
    addons.py update

    addons.py            (no arguments) opens the window instead

This exists because the addon managers for this realm either cannot be pointed
at an arbitrary repo or phone home about what you install. It does neither: it
talks to exactly the hosts you name in the manifest and to nothing else, and it
has no analytics of any kind. `check` and `update` hit api.github.com only for
the addons you have actually bound to a GitHub repo.

Stdlib only -- no pip install, nothing to keep updated.

HOW IT FITS TOGETHER

  init    remembers where Interface/AddOns is
  scan    reads every addon already installed there and writes them into the
          manifest, guessing a source from each .toc where it can
  set     binds one addon to where its updates should come from
  update  brings every bound addon up to date

Sources:

  local:<path>        a folder on this disk -- a checkout of this repo, or any
                      other. Installed as a SYMLINK by default, which makes
                      `git pull` the entire update: nothing to re-copy, and the
                      client can never be running something other than what is
                      checked out. `--copy` if you would rather have real files.
  github:owner/repo   latest GitHub release, preferring an attached .zip and
                      falling back to the source archive
  github:owner/repo@branch   that branch's current head instead of a release
  github:owner/repo#Folder   ONE addon out of a repository that holds several.
                      Only that folder is installed, and its version is the
                      last commit that touched it -- so the other addons in the
                      repo neither get installed nor make this one look out of
                      date. `@branch#Folder` combines with the above.
  unmanaged           leave it alone

A github.com link works anywhere owner/repo does, including a link to a folder:
https://github.com/owner/repo/tree/main/MyAddon means that addon, on that branch.

The manifest lives outside this repo, in
$XDG_CONFIG_HOME/wow-addons/manifest.json, because it holds your disk paths and
this repo is public.
"""


BOLD, YELLOW, RED, DIM, RESET = "\033[1m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = YELLOW = RED = DIM = RESET = ""


def step(msg: str) -> None:
    print(f"\n{BOLD}== {msg}{RESET}")


def note(msg: str = "") -> None:
    print(f"   {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}   ! {msg}{RESET}", file=sys.stderr)


def show(level: str, message: str) -> None:
    (warn if level == "warn" else note)(message)


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_init(args, state: dict) -> None:
    target = core.find_addons_dir(Path(args.path))
    state["addons_dir"] = str(target)
    core.save(state)
    step("WoW folder set")
    note(str(target))
    note("")
    note("Next:  addons.py scan     (read what is already installed)")


def cmd_scan(args, state: dict) -> None:
    root = core.addons_dir(state)
    step(f"Scanning {root}")
    installed, guessed = core.rescan(state, root)
    core.save(state)
    note(f"{installed} addon folder(s) installed")
    if guessed:
        note(f"{guessed} with a source found or suggested -- see `addons.py list`")
    note("")
    note("Bind one with:  addons.py set <Addon> github:owner/repo")
    note("            or: addons.py set <Addon> local:/path/to/folder")


def cmd_list(args, state: dict) -> None:
    root = core.addons_dir(state)
    entries = state.get("addons", {})
    if not entries:
        core.die("nothing scanned yet. Run:  addons.py scan")

    step(f"{len(entries)} addon(s) in {root}")
    width = max(len(n) for n in entries)
    for name in sorted(entries, key=str.lower):
        entry = entries[name]
        source = entry.get("source", "unmanaged")
        installed = entry.get("installed") or entry.get("toc_version") or ""
        latest = entry.get("latest")
        if latest and latest != entry.get("installed"):
            installed = f"{installed} -> {latest}"
        flags = []
        if entry.get("missing"):
            flags.append("NOT INSTALLED")
        if source == "unmanaged" and entry.get("suggested"):
            flags.append(f"suggested: {entry['suggested']}")
        tail = f"  {DIM}({', '.join(flags)}){RESET}" if flags else ""
        print(f"   {name:<{width}}  {core.tilde(source):<44} {installed}{tail}")


def cmd_set(args, state: dict) -> None:
    backup = False if args.no_backup else (True if args.backup else None)
    entry, local_path = core.set_source(
        state, args.addon, args.source, copy=args.copy, backup=backup
    )
    core.save(state)

    step(f"{args.addon} -> {core.tilde(entry['source'])}")
    # The folder's own name is what lands in AddOns, not the name you typed --
    # the client matches folder to .toc, so renaming on the way in would break it.
    if local_path is not None and local_path.name != args.addon:
        warn(f"that folder is named {local_path.name}, so it installs as that, not as {args.addon}.")
    if local_path is not None and entry["mode"] == "link":
        note("linked, so `git pull` in that folder is the whole update.")
    if not entry.get("backup", True):
        note("backups off: an existing folder is replaced outright.")
    note("Run:  addons.py update " + args.addon)


def cmd_accept(args, state: dict) -> None:
    """Take every source `scan` suggested, in one go."""
    step("Accepting suggested sources")
    taken = core.accept_suggestions(state)
    for name, source in taken:
        note(f"{name} -> {source}")
    core.save(state)
    note(f"{len(taken)} bound" if taken else "nothing was suggested")


def cmd_update(args, state: dict) -> None:
    root = core.addons_dir(state)
    entries = state.get("addons", {})
    names = args.addons or sorted(entries, key=str.lower)

    step("Update" + (" (dry run)" if args.dry_run else ""))
    changed = skipped = 0
    failed: list[str] = []
    for name in names:
        entry = entries.get(name)
        if entry is None:
            warn(f"{name}: not in the manifest -- run `addons.py scan` first")
            continue

        result = core.update_addon(
            name, entry, root, force=args.force, dry_run=args.dry_run, check=args.check
        )
        for level, message in result.notes:
            show(level, message)

        if result.outcome == core.UNMANAGED:
            skipped += 1
        elif result.outcome == core.UP_TO_DATE:
            note(f"{DIM}{name}: {result.detail}{RESET}")
            skipped += 1
        elif result.outcome == core.FAILED:
            warn(f"{name}: {result.detail}")
            failed.append(name)
        else:
            note(f"{name}: {result.detail}")
            if args.dry_run:
                for folder in result.folders:
                    note(f"would install {folder}")
            changed += 1

    if not args.dry_run and not args.check:
        core.save(state)
    step(f"Done — {changed} changed, {skipped} unchanged/unmanaged, {len(failed)} failed")
    if changed:
        note("Restart the client, or /reload, to pick the changes up.")
    if failed:
        note(f"failed: {', '.join(failed)}")
        raise SystemExit(1)


def cmd_where(args, state: dict) -> None:
    reading = core.manifest_to_read()
    note(f"manifest:  {core.MANIFEST}")
    if reading != core.MANIFEST:
        # Windows, mid-migration: say which file the numbers came from, because
        # otherwise `where` points at an empty file and looks like it is lying.
        note(f"           (still reading {reading}; the next write moves it)")
    note(f"AddOns:    {state.get('addons_dir') or '(not set)'}")


def cmd_gui(args, state: dict) -> None:
    from . import gui

    gui.main()


# ── argument parsing ─────────────────────────────────────────────────────────


def build_parser(prog: str = "addons.py", epilog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Bind each installed WoW addon to the repo you want it updated from.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG if epilog is None else epilog,
    )
    # Before the subparsers, so `--version` works on its own. Every bug report
    # about a downloaded binary starts with "which build?", and a user who
    # double-clicked an AppImage has no other way to answer that.
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
        help="print the version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="point at your WoW folder")
    p.add_argument("path", help="the WoW folder, or Interface/AddOns directly")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="read every addon already installed")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list", help="show every addon and its source")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="bind one addon to a source")
    p.add_argument("addon")
    p.add_argument("source", help="local:/path | github:owner/repo | github:owner/repo@branch | unmanaged")
    p.add_argument("--copy", action="store_true", help="copy real files instead of symlinking (local: only)")
    # A folder this tool installed is replaced without a copy either way; these
    # only decide what happens to files it did not put there.
    p.add_argument("--no-backup", action="store_true",
                   help="replace an existing folder outright instead of keeping a copy")
    p.add_argument("--backup", action="store_true",
                   help="keep a copy of an existing folder as <Name>.replaced (the default)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("accept", help="take every source that scan suggested")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("update", help="bring bound addons up to date")
    p.add_argument("addons", nargs="*", help="default: all of them")
    p.add_argument("--check", action="store_true", help="report what is out of date, download nothing")
    p.add_argument("--dry-run", action="store_true", help="do everything but write")
    p.add_argument("--force", action="store_true", help="reinstall even if the version matches")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("where", help="print the manifest and AddOns paths")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("gui", help="open the window instead of using the terminal")
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None, *, prog: str = "addons.py", epilog: str | None = None) -> None:
    args = build_parser(prog, epilog).parse_args(argv)
    try:
        args.func(args, core.load())
    except Fail as exc:
        print(f"{RED}\nFAILED: {exc}{RESET}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
