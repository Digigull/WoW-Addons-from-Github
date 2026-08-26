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

SEVERAL WOW FOLDERS

A vanilla server, a Wrath one and retail are separate installs: separate AddOns
directories, separate bindings, nothing shared. `init` again adds one rather
than replacing the first.

  addons.py init ~/Games/Vanilla --name Vanilla
  addons.py installs                     what is known, * marks the one in use
  addons.py use Vanilla                  switch
  addons.py update --install Wrath       aim ONE run elsewhere, without switching
  addons.py forget Vanilla               stop tracking it (deletes no game files)

The same addon may be bound differently in each -- a different branch, or a
different folder of the same repository -- which is usually the point.

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


def selected(args, state: dict) -> dict:
    """The install this command acts on: --install, else the current one."""
    name = getattr(args, "install", None)
    return core.pick(state, name) if name else core.current(state)


def cmd_init(args, state: dict) -> None:
    target = core.find_addons_dir(Path(args.path))
    name = core.add_install(state, target, args.name)
    core.save(state)
    step(f"WoW folder set: {name}")
    note(str(target))
    if len(core.installs(state)) > 1:
        note("")
        note(f"{len(core.installs(state))} installs known. `addons.py installs` lists them,")
        note("`addons.py use <name>` switches, and `--install <name>` targets one run.")
    note("")
    note("Next:  addons.py scan     (read what is already installed)")


def cmd_installs(args, state: dict) -> None:
    known = core.installs(state)
    if not known:
        core.die("no WoW folder set yet. Run:  addons.py init /path/to/your/wow/folder")
    current = core.current_name(state)
    step(f"{len(known)} install(s)")
    width = max(len(n) for n in known)
    for name in sorted(known, key=str.lower):
        install = known[name]
        entries = install.get("addons", {})
        bound = sum(1 for e in entries.values() if e.get("source", "unmanaged") != "unmanaged")
        marker = f"{BOLD}*{RESET}" if name == current else " "
        counts = f"{len(entries)} addon(s), {bound} bound" if entries else "not scanned yet"
        note(f"{marker} {name:<{width}}  {core.tilde(install.get('addons_dir') or '(not set)')}")
        note(f"  {'':<{width}}  {DIM}{counts}{RESET}")
    note("")
    note("* is the one commands act on. Switch with `use`, or aim one run with --install.")


def cmd_use(args, state: dict) -> None:
    install = core.use(state, args.name)
    core.save(state)
    step(f"now using {args.name}")
    note(core.tilde(install.get("addons_dir") or "(not set)"))


def cmd_forget(args, state: dict) -> None:
    """Stop tracking an install. Deletes nothing in the game folder."""
    core.forget_install(state, args.name)
    core.save(state)
    step(f"forgot {args.name}")
    note("Nothing in that WoW folder was touched -- only this tool's record of it.")


def cmd_scan(args, state: dict) -> None:
    install = selected(args, state)
    root = core.addons_dir(install)
    step(f"Scanning {root}")
    installed, guessed, forgotten = core.rescan(install, root)
    core.save(state)
    note(f"{installed} addon folder(s) installed")
    if forgotten:
        note(f"{forgotten} unmanaged addon(s) no longer on disk -- dropped from the list")
    if guessed:
        note(f"{guessed} with a source found or suggested -- see `addons.py list`")
    note("")
    note("Bind one with:  addons.py set <Addon> github:owner/repo")
    note("            or: addons.py set <Addon> local:/path/to/folder")


def cmd_list(args, state: dict) -> None:
    install = selected(args, state)
    root = core.addons_dir(install)
    entries = install.get("addons", {})
    if not entries:
        core.die("nothing scanned yet. Run:  addons.py scan")

    step(f"{len(entries)} addon(s) in {root}")
    width = max(len(n) for n in entries)
    for name in core.display_order(entries):
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
        selected(args, state), args.addon, args.source, copy=args.copy, backup=backup
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
    taken = core.accept_suggestions(selected(args, state))
    for name, source in taken:
        note(f"{name} -> {source}")
    core.save(state)
    note(f"{len(taken)} bound" if taken else "nothing was suggested")


def cmd_update(args, state: dict) -> None:
    install = selected(args, state)
    root = core.addons_dir(install)
    entries = install.get("addons", {})
    names = args.addons or core.display_order(entries)
    # The flag turns it on for one run; the window's checkbox is what sets it
    # for good, and both front ends read the same answer.
    no_api = args.no_api or core.checks_without_api(install)

    # Waiting on GitHub with nothing on screen reads as a hang, and the pacing
    # only exists to be waited on, so it says so.
    core.set_wait_hook(lambda seconds, why: note(f"{DIM}waiting {seconds:.0f}s — {why}{RESET}"))
    # A run is one pass: ask each thing once, and write down what GitHub said
    # about it so the next run can ask for free.
    core.begin_run()

    step("Update" + (" (dry run)" if args.dry_run else "")
         + (" — without the GitHub API" if no_api else ""))
    changed = skipped = 0
    failed: list[str] = []
    for name in names:
        entry = entries.get(name)
        if entry is None:
            warn(f"{name}: not in the manifest -- run `addons.py scan` first")
            continue

        result = core.update_addon(
            name, entry, root, force=args.force, dry_run=args.dry_run,
            check=args.check, no_api=no_api,
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
    # Written even for --check and --dry-run: no addon file was touched, but
    # the ETags this run learned are exactly what makes the next one free.
    core.end_run()
    left = core.quota_left()
    budget = f", {left} GitHub call(s) left this hour" if left is not None else ""
    step(f"Done — {changed} changed, {skipped} unchanged/unmanaged, {len(failed)} failed{budget}")
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
    known = core.installs(state)
    if not known:
        note("AddOns:    (not set)")
        return
    label = "AddOns:"
    for name in sorted(known, key=str.lower):
        marker = "*" if name == core.current_name(state) else " "
        note(f"{label:<9}{marker} {name}: {known[name].get('addons_dir') or '(not set)'}")
        label = ""


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

    def targeted(name: str, **kw):
        """A subcommand that acts on one install, and can be aimed at another."""
        made = sub.add_parser(name, **kw)
        made.add_argument(
            "--install", metavar="NAME",
            help="act on this install for this run only, without switching to it",
        )
        return made

    p = sub.add_parser("init", help="point at a WoW folder (run it again for a second one)")
    p.add_argument("path", help="the WoW folder, or Interface/AddOns directly")
    p.add_argument("--name", help="what to call this install (default: the folder's own name)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("installs", help="list every WoW folder this tool knows")
    p.set_defaults(func=cmd_installs)

    p = sub.add_parser("use", help="switch which install commands act on")
    p.add_argument("name")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("forget", help="stop tracking an install (deletes no game files)")
    p.add_argument("name")
    p.set_defaults(func=cmd_forget)

    p = targeted("scan", help="read every addon already installed")
    p.set_defaults(func=cmd_scan)

    p = targeted("list", help="show every addon and its source")
    p.set_defaults(func=cmd_list)

    p = targeted("set", help="bind one addon to a source")
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

    p = targeted("accept", help="take every source that scan suggested")
    p.set_defaults(func=cmd_accept)

    p = targeted("update", help="bring bound addons up to date")
    p.add_argument("addons", nargs="*", help="default: all of them")
    p.add_argument("--check", action="store_true", help="report what is out of date, download nothing")
    p.add_argument("--dry-run", action="store_true", help="do everything but write")
    p.add_argument("--force", action="store_true", help="reinstall even if the version matches")
    p.add_argument("--no-api", action="store_true",
                   help="check without the GitHub API: follows branches, not releases")
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
