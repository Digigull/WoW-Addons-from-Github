"""The engine: manifest, scanning, sources, install.

Nothing in here prints. That is the whole point of the split -- a GUI needs to
put "could not reach GitHub" in a table row, not on stdout, and the moment the
engine writes to a terminal it stops being usable from a window.

Two escape hatches replace printing, and both are optional so a caller that
does not care can ignore them:

    progress(stage, detail)   what is happening right now ("downloading", ...)
    report(level, message)    a notice worth keeping ("moving folder aside")

`Fail` carries its message rather than printing it, because whether a failure
is fatal depends on the caller: for `init` it ends the run, inside an update it
is one addon out of many and the rest must still go.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

def on_windows() -> bool:
    return os.name == "nt"


def default_config_dir(windows: bool | None = None) -> Path:
    """Where this platform expects an application to keep its settings.

    %APPDATA% on Windows; $XDG_CONFIG_HOME (or ~/.config) everywhere else.
    Windows landing in ~/.config was a straight bug -- it works, but it is not
    a place a Windows user or their backup software would ever think to look.

    `windows` is a parameter rather than a bare os.name check so that a test can
    ask what the other platform would do. Faking os.name globally is not an
    option: pathlib reads it to pick which kind of Path to build, so a test that
    set it would break every path in the process.
    """
    if on_windows() if windows is None else windows:
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "wow-addons"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wow-addons"


CONFIG_DIR = default_config_dir()
MANIFEST = CONFIG_DIR / "manifest.json"
# Where Windows used to put it. Read from here if nothing is in the new place
# yet, so upgrading does not silently look like "you have no addons bound".
LEGACY_WINDOWS_MANIFEST = Path.home() / ".config" / "wow-addons" / "manifest.json"
USER_AGENT = "wow-addons-sync (stdlib urllib)"


class Fail(Exception):
    """A problem worth stopping for. Carries its message; never prints it."""


def die(msg: str) -> "NoReturn":  # noqa: F821
    raise Fail(msg)


def _nothing(*_args) -> None:
    """The default `progress`/`report`: accept anything, do nothing."""


# ── manifest ─────────────────────────────────────────────────────────────────


def manifest_to_read(windows: bool | None = None) -> Path:
    """The manifest to load, which is not always the one we would write.

    On Windows the location moved to %APPDATA%; anyone who used the tool before
    that has their real manifest under ~/.config. Reading from the old place
    when the new one is empty makes the move invisible, and the next `save`
    writes to the new location and completes the migration on its own.
    """
    if MANIFEST.exists() or not (on_windows() if windows is None else windows):
        return MANIFEST
    return LEGACY_WINDOWS_MANIFEST if LEGACY_WINDOWS_MANIFEST.exists() else MANIFEST


# ── installs ─────────────────────────────────────────────────────────────────
# One person can have several WoW folders: a vanilla server, a Wrath one,
# retail. They share nothing -- different AddOns directories, and an addon
# bound in one says nothing about the same addon in another, which may want a
# different branch or a different folder of the same repository entirely.
#
# So the manifest holds several INSTALLS, and an install has exactly the shape
# the whole manifest used to have:
#
#     {"addons_dir": "...", "addons": {name: entry}}
#
# That is deliberate and it is why this change is small: every function that
# took the old state and reached for `addons` or `addons_dir` now takes one
# install and is otherwise untouched. The outer manifest is a new, thin layer
# above them.

MANIFEST_VERSION = 2


def blank_install() -> dict:
    return {"addons_dir": None, "addons": {}}


def is_empty_install(install: dict) -> bool:
    """Nothing pointed at and nothing recorded -- a placeholder, not an install."""
    return not install.get("addons_dir") and not install.get("addons")


def install_name_for(directory: str | None, taken=()) -> str:
    """A name to file an install under, from its folder.

    `.../Ascension/Interface/AddOns` becomes "Ascension" -- the folder people
    already call the install by, so nobody has to invent a label to get started.
    """
    name = "default"
    if directory:
        parts = [p for p in Path(directory).parts if p not in ("/", "\\")]
        skip = {"addons", "interface"}
        meaningful = [p for p in parts if p.lower() not in skip]
        if meaningful:
            name = meaningful[-1]
    candidate, counter = name, 1
    while candidate in taken:
        counter += 1
        candidate = f"{name} ({counter})"
    return candidate


def migrate(state: dict) -> dict:
    """Bring a manifest written before installs existed up to date.

    Read-time only, and non-destructive: the file on disk is not rewritten
    until something else saves it, so an older build reading the same manifest
    keeps working right up until this one writes.
    """
    if "installs" in state:
        return state
    install = {
        "addons_dir": state.get("addons_dir"),
        "addons": state.get("addons", {}),
    }
    if is_empty_install(install):
        # A manifest that never got as far as `init`. Filing that under a name
        # would leave a phantom install in the list forever, and the first real
        # one would arrive as the second entry.
        return {"version": MANIFEST_VERSION, "installs": {}}
    name = install_name_for(install["addons_dir"])
    return {"version": MANIFEST_VERSION, "current": name, "installs": {name: install}}


def one_install(install: dict, called: str) -> dict:
    """Guard against being handed the whole manifest instead of one install.

    An install and the old whole-manifest shape are deliberately identical, so
    the mistake is easy and, worse, quiet: `set_source(state, ...)` on a
    manifest would `setdefault("addons", {})` at the top level and write the
    binding into a key nothing ever reads again. The addon would appear to have
    been bound and would never update.

    A TypeError rather than a Fail: this is a contract violation in calling
    code, not something a user did, and it should stop a test dead rather than
    be reported in a status bar.
    """
    if "installs" in install:
        raise TypeError(
            f"{called}() takes one install, not the whole manifest; "
            "pass core.current(state) or core.pick(state, name)"
        )
    return install


def installs(state: dict) -> dict:
    return state.setdefault("installs", {})


def current_name(state: dict) -> str:
    """The install commands act on, chosen stably when the record is unclear.

    Falls back to the first install by name rather than to whatever a dict
    happens to yield first: an arbitrary answer here would silently update a
    different WoW folder than the last run did.
    """
    known = installs(state)
    name = state.get("current")
    if name in known:
        return name
    if known:
        return sorted(known)[0]
    name = name or "default"
    known[name] = blank_install()
    state["current"] = name
    return name


def current(state: dict) -> dict:
    """The install to act on. Every command works through this."""
    return installs(state).setdefault(current_name(state), blank_install())


def pick(state: dict, name: str) -> dict:
    """One install by name, without changing which one is current.

    `--install` targets a command at another WoW folder for that one run. It
    would be a nasty surprise if a single `--install Wrath update` quietly left
    every later command pointed at Wrath.
    """
    known = installs(state)
    if name not in known:
        options = ", ".join(sorted(known)) or "none yet"
        die(f"no install called '{name}'. There is: {options}")
    return known[name]


def use(state: dict, name: str) -> dict:
    """Switch which install is current, and mean it -- the caller saves."""
    install = pick(state, name)
    state["current"] = name
    return install


def add_install(state: dict, directory: Path, name: str | None = None) -> str:
    """Remember another WoW folder, and switch to it. Returns its name.

    Pointing an existing install at the same folder again is an update, not a
    duplicate: re-running init after moving the game should not leave two
    entries racing to manage one directory.
    """
    known = installs(state)
    target = str(directory)
    for existing, install in known.items():
        if install.get("addons_dir") == target and (name is None or name == existing):
            state["current"] = existing
            return existing
    name = name or install_name_for(target, taken=known)
    if name in known:
        known[name]["addons_dir"] = target
    else:
        known[name] = {"addons_dir": target, "addons": {}}
    # Sweep up any placeholder a bare `load` left behind, so the list shows the
    # WoW folders somebody actually has and nothing else.
    for empty in [n for n, i in known.items() if n != name and is_empty_install(i)]:
        del known[empty]
    state["current"] = name
    return name


def forget_install(state: dict, name: str) -> None:
    """Stop tracking one install. Touches no files in the game folder."""
    known = installs(state)
    if name not in known:
        die(f"no install called '{name}'")
    del known[name]
    if state.get("current") == name:
        state.pop("current", None)
        if known:
            state["current"] = sorted(known)[0]


def load(windows: bool | None = None) -> dict:
    path = manifest_to_read(windows)
    if not path.exists():
        return migrate(blank_install())
    try:
        return migrate(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        die(f"manifest is not valid JSON ({exc}). Fix or delete {path}")


def save(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write-and-move: a manifest half-written by a Ctrl-C is worse than a stale
    # one, since it is the only record of what you bound where.
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(MANIFEST)


def checks_without_api(install: dict) -> bool:
    """Whether this install checks without the GitHub REST API."""
    return bool(install.get("no_api"))


def set_checks_without_api(install: dict, no_api: bool) -> None:
    install["no_api"] = bool(no_api)


def addons_dir(install: dict) -> Path:
    """The AddOns folder of one install -- not of the manifest as a whole."""
    state = one_install(install, "addons_dir")
    if not state.get("addons_dir"):
        die("no WoW folder set yet. Run:  addons.py init /path/to/your/wow/folder")
    path = Path(state["addons_dir"])
    if not path.is_dir():
        die(f"the remembered AddOns folder is gone: {path}\nRe-run init.")
    return path


def new_entry(name: str) -> dict:
    return {"source": "unmanaged", "mode": "link", "installed": None, "folders": [name]}


# ── links ────────────────────────────────────────────────────────────────────
# A `local:` source is installed as a link so that `git pull` in the checkout is
# the whole update. On Unix that is a symlink. On Windows os.symlink needs
# administrator rights or Developer Mode, which is not a reasonable thing to ask
# of someone who wants to update an addon -- so a directory JUNCTION is used
# instead. It needs no privileges at all and the client cannot tell the
# difference.
#
# Junctions differ from symlinks in both directions, and both differences bite:
#
#   creating   mklink /J, because CreateJunction is private in CPython
#   removing   os.rmdir, not os.unlink -- unlink refuses a directory
#
# is_link() is the one that actually matters for safety. Everything that
# replaces an installed addon asks "is it a link?" first and calls shutil.rmtree
# if the answer is no. Get that wrong for a junction and rmtree walks THROUGH it
# and deletes the user's source checkout. That is why this checks the reparse
# tag rather than trusting Path.is_symlink(), whose treatment of junctions has
# not been consistent across Python versions.


def is_link(path: Path) -> bool:
    """True for a symlink, and for a Windows directory junction."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    if os.name != "nt":
        return False
    try:
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError, ValueError):
        return False


def link_target(path: Path) -> str | None:
    """Where a link points, or None if it is not one (or cannot be read)."""
    if not is_link(path):
        return None
    try:
        target = os.readlink(path)
    except OSError:
        return None
    # readlink on a junction hands back an extended-length path. It is correct
    # but it is not what anyone typed, and it should not be what a listing shows.
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\"):
        if target.startswith(prefix):
            target = target[len(prefix):]
            break
    return target


def make_link(source: Path, destination: Path) -> None:
    """Point destination at source. Junction on Windows, symlink elsewhere."""
    if os.name != "nt":
        destination.symlink_to(source, target_is_directory=True)
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not destination.exists():
        detail = (result.stderr or result.stdout or "").strip() or f"mklink exited {result.returncode}"
        die(f"could not link {destination} -> {source}: {detail}")


def remove_link(path: Path) -> None:
    """Detach a link without touching what it points at."""
    if os.name == "nt" and path.is_dir():
        # A junction is a directory entry: unlink refuses it, rmdir removes the
        # reparse point and leaves the target alone. rmdir on a symlink-to-dir
        # behaves the same way, so this covers both without asking which it is.
        os.rmdir(path)
    else:
        path.unlink()


# ── .toc parsing ─────────────────────────────────────────────────────────────
# 3.3.5 .toc headers are `## Key: Value`. Only a handful matter here, and every
# one of them is optional -- plenty of addons ship with nothing but a Title.

TOC_LINE = re.compile(r"^##\s*([^:]+?)\s*:\s*(.*?)\s*$")
GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", re.I)


def read_toc(toc: Path) -> dict:
    fields: dict[str, str] = {}
    # WoW .tocs are latin-1 or UTF-8 and some carry a BOM; none of that should
    # be able to stop a scan, so decode permissively.
    for line in toc.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = TOC_LINE.match(line)
        if match:
            fields[match.group(1).strip().lower()] = match.group(2).strip()
    return fields


def find_toc(folder: Path) -> Path | None:
    """The .toc the game would load for this folder, or None if there is none.

    WoW loads AddOns/<Folder>/<Folder>.toc and nothing else -- but it matches
    that name the way its filesystem does, which is case-insensitively on
    Windows and, for a Wine install on Linux, case-insensitively too. So
    PlayerbotManager/Playerbotmanager.toc is an addon the game loads happily,
    and a scan that only asked for the exact spelling reported the folder as
    not an addon at all and left it out of the list entirely.
    """
    exact = folder / f"{folder.name}.toc"
    if exact.is_file():
        return exact
    wanted = f"{folder.name.lower()}.toc"
    try:
        for child in folder.iterdir():
            if child.name.lower() == wanted and child.is_file():
                return child
    except OSError:
        pass
    return None


def guess_source(fields: dict) -> str | None:
    """Pull a github:owner/repo out of a .toc if the author left one."""
    for key in ("x-repository", "x-website", "x-project-url", "x-github", "x-curse-project-url"):
        value = fields.get(key, "")
        match = GITHUB_URL.search(value)
        if match:
            return f"github:{match.group(1)}/{match.group(2)}"
    return None


def tilde(path: str) -> str:
    """Shorten $HOME to ~ so the list stays readable on one line."""
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def strip_colours(text: str) -> str:
    """Titles are full of |cff.... escapes; they are noise in a table."""
    return re.sub(r"\|c[0-9a-fA-F]{8}|\|r|\|T.-\|t", "", text).strip()


# ── scanning an AddOns folder ────────────────────────────────────────────────


def scan_installed(root: Path) -> dict[str, dict]:
    """Every folder in AddOns that looks like an addon, with its .toc facts.

    An addon folder is one holding <FolderName>.toc. Blizzard_* folders are the
    stock UI and are deliberately included -- you may not want to manage them,
    but leaving them out of the listing makes the scan look like it missed them.
    """
    found: dict[str, dict] = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        toc = find_toc(child)
        if toc is None:
            continue
        fields = read_toc(toc)
        found[child.name] = {
            "title": strip_colours(fields.get("title", child.name)),
            "version": fields.get("version", ""),
            "guess": guess_source(fields),
            "is_link": is_link(child),
            "link_target": link_target(child),
        }
    return found


def folder_problem(folder: Path) -> str | None:
    """Why a folder in AddOns is not being listed as an addon, if you can fix it.

    A scan that silently drops a folder is indistinguishable, from the outside,
    from a scan that is broken: the addon is right there in AddOns and the list
    says it is not. Most skipped folders are nothing -- an empty Blizzard_*
    stub, a folder of notes -- and saying so about each of those is noise. The
    ones worth a word are the folders that plainly hold an addon and still will
    not load, because the fix is a rename or a move the person can do in a file
    manager in ten seconds.

    Returns None for a folder that is simply not an addon.
    """
    try:
        children = list(folder.iterdir())
    except OSError as exc:
        return f"cannot be read ({exc.strerror or exc})"

    # Not is_file(): a broken symlink, or a directory someone named MyAddon.toc,
    # is exactly the case where the name is right and the game still loads
    # nothing -- and where "no addon here" is the least useful thing to say.
    named = [c for c in children if c.name.lower() == f"{folder.name.lower()}.toc"]
    if named:
        return (f"'{named[0].name}' is there but is not a readable file -- "
                "a broken shortcut, or a folder with that name.")

    files = [child for child in children if child.is_file()]
    hidden = [f.name for f in files if f.name.lower() == f"{folder.name.lower()}.toc.txt"]
    if hidden:
        return (f"holds '{hidden[0]}' -- Windows hid the real extension. "
                f"Rename it to '{folder.name}.toc'.")

    tocs = sorted(f.name for f in files if f.name.lower().endswith(".toc"))
    if tocs:
        stem = tocs[0][:-4]
        return (f"holds '{tocs[0]}' but no '{folder.name}.toc' -- the game only loads a "
                f".toc named after its folder. Rename the folder to '{stem}', "
                f"or the file to '{folder.name}.toc'.")

    nested = sorted(
        child.name for child in children if child.is_dir() and find_toc(child) is not None
    )
    if nested:
        listed = ", ".join(nested[:3]) + (", ..." if len(nested) > 3 else "")
        return (f"the addon{'s are' if len(nested) > 1 else ' is'} one folder deeper, in "
                f"{folder.name}/ ({listed}) -- move {'them' if len(nested) > 1 else 'it'} "
                f"up into AddOns.")

    code = sorted(f.name for f in files if f.name.lower().endswith((".lua", ".xml")))
    if code:
        return (f"holds {code[0]} but no '{folder.name}.toc' -- the game loads no folder "
                "without one, so this is not an addon it can run.")

    return None


def scan_problems(root: Path) -> dict[str, str]:
    """Folders in AddOns that look like an addon and are not one, and why."""
    problems: dict[str, str] = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or find_toc(child) is not None:
            continue
        reason = folder_problem(child)
        if reason:
            problems[child.name] = reason
    return problems


def rescan(state: dict, root: Path) -> tuple[int, int, int]:
    """Fold what is on disk into the manifest. (installed, guessed, forgotten).

    Does not save -- the caller decides when to write, which matters for a GUI
    that may want to scan speculatively.
    """
    one_install(state, "rescan")
    installed = scan_installed(root)
    entries = state.setdefault("addons", {})

    guessed = 0
    for name, facts in installed.items():
        entry = entries.setdefault(name, new_entry(name))
        entry["title"] = facts["title"]
        entry["toc_version"] = facts["version"]
        entry.pop("missing", None)

        # A folder that is already a symlink is being managed by hand; record
        # where it points so the listing tells the truth instead of saying
        # unmanaged.
        if facts["is_link"] and entry["source"] == "unmanaged":
            entry["source"] = f"local:{facts['link_target']}"
            entry["mode"] = "link"
            guessed += 1
        elif entry["source"] == "unmanaged" and facts["guess"]:
            entry["suggested"] = facts["guess"]
            guessed += 1

    # Anything in the manifest that is no longer on disk. What happens next
    # depends on whether the row holds a decision of yours.
    #
    # A bound row is KEPT and flagged. "Not installed" is a real state, not an
    # error: it is how an addon you have bound but not yet fetched appears, and
    # how one you deleted to force a clean reinstall appears between the delete
    # and the update. Dropping it would throw away the binding you set, which
    # is the only thing in this manifest you cannot get back by scanning again.
    #
    # An unmanaged row holds nothing of yours -- it is a note that a folder was
    # once there, and the folder is not there any more. Keeping it means the
    # list slowly fills with addons you deleted on purpose and cannot get rid
    # of, because nothing else in this tool removes a row.
    forgotten = 0
    for name in list(entries):
        if name in installed:
            continue
        if entries[name].get("source", "unmanaged") == "unmanaged":
            del entries[name]
            forgotten += 1
        else:
            entries[name]["missing"] = True

    return len(installed), guessed, forgotten


# ── sources ──────────────────────────────────────────────────────────────────


# What people actually have on the clipboard when they mean "this addon".
# Ordinary browsing URLs, the clone URLs GitHub offers, and the SSH form.
REPO_URL = re.compile(
    r"""^(?:https?://|git@|ssh://git@)?              # scheme, or none at all
         (?:www\.)?github\.com[/:]                    # the host
         (?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)         # the part that matters
         (?:\.git)?                                    # clone URLs end this way
         (?:/(?:tree|blob)/(?P<branch>[^/#?]+)         # browsing a branch
            (?P<folder>(?:/[^#?]*)?))?                 # ...and a folder inside it
         /*(?:[#?].*)?$                                # trailing slash, anchor, query
      """,
    re.I | re.X,
)
# owner/repo, optionally naming one folder inside it: owner/repo#Sub/Folder.
BARE_REPO = re.compile(
    r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?:#(?P<folder>[^#]+))?$"
)


def parse_repo(text: str) -> tuple[str, str | None, str | None] | None:
    """Pull (owner/repo, branch, folder) out of whatever the user pasted.

    Accepts `owner/repo` and every shape of GitHub URL somebody is likely to
    have copied: the page they were looking at, the green Code button's HTTPS
    and SSH URLs, and a link to a specific branch. Returns None if it is not a
    GitHub repository at all -- a CurseForge page, say -- so the caller can say
    so rather than storing something that will never resolve.

    Telling somebody who just pasted a working URL to retype it as owner/repo is
    a small insult that this tool has no reason to offer.

    The folder matters for a repository holding several addons. Clicking into
    one on github.com gives a `/tree/<branch>/<folder>` URL, and that is both
    the obvious thing to paste and an exact statement of which addon is meant --
    so it is read rather than discarded.
    """
    text = text.strip()
    if not text:
        return None
    for pattern in (REPO_URL, BARE_REPO):
        match = pattern.match(text)
        if match:
            found = match.groupdict()
            folder = (found.get("folder") or "").strip("/")
            return f"{found['owner']}/{found['repo']}", found.get("branch"), folder or None
    return None


ACCOUNT_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<name>[\w.-]+)/*(?:[#?].*)?$", re.I
)


def github_account(text: str) -> str | None:
    """The account name, if this is a github.com user or organisation page.

    `github.com/Ascension-Addons` is a perfectly good link that names no
    repository at all, and it is an easy thing to paste when the addons you
    want are published by an organisation. Saying only "not a GitHub
    repository" about it is true and unhelpful -- it reads as though the link
    is broken, when the real answer is "open the addon you want and paste that
    address instead".
    """
    match = ACCOUNT_URL.match(text.strip())
    return match.group("name") if match else None


def looks_like_a_repo(source: str) -> bool:
    """Is this a GitHub repository rather than a path, with no prefix to say so?

    `owner/repo` and `some/folder` are the same shape, so the answer cannot come
    from the text alone. A github.com URL is never a path and is always taken as
    one. A bare `a/b` is taken as a repo only when it cannot be a path: not
    rooted, not relative, and not something that exists on this disk.

    When in doubt this says no, and the user gets the "expected local: or
    github:" message rather than a source that silently means the wrong thing.
    """
    if "github.com" in source.lower():
        return True
    if source.startswith((".", "/", "~", "\\")) or Path(source).expanduser().exists():
        return False
    return BARE_REPO.match(source) is not None


def parse_source(source: str) -> tuple[str, str]:
    """'github:owner/repo@branch' -> ('github', 'owner/repo@branch')."""
    source = source.strip()
    if source == "unmanaged":
        return "unmanaged", ""
    # A pasted URL has its own colon, so this has to come before the split.
    if not source.startswith(("local:", "github:")) and looks_like_a_repo(source):
        return "github", source
    if ":" not in source:
        die(
            f"cannot read source '{source}'. Expected one of:\n"
            "     local:/path/to/folder\n"
            "     github:owner/repo\n"
            "     github:owner/repo@branch\n"
            "     https://github.com/owner/repo\n"
            "     unmanaged"
        )
    kind, rest = source.split(":", 1)
    if kind not in ("local", "github"):
        die(f"unknown source type '{kind}' (expected local, github or unmanaged)")
    return kind, rest


def resolve_source(addon: str, source: str) -> tuple[str, str, Path | None]:
    """Normalise what a user typed into what goes in the manifest.

    Returns (source, kind, local_path). `local:.` is the common case -- this
    repo, from wherever you happened to run it -- so a local path is made
    absolute here rather than being re-resolved later against a different
    working directory. Pointing at a repo root rather than an addon folder is
    also common enough to handle: look inside for <addon>/<addon>.toc.

    Shared by both front ends so `set` in the terminal and Save in the dialog
    cannot disagree about what a path means.
    """
    kind, rest = parse_source(source)
    if kind == "github":
        # `set X github:https://github.com/o/r` is a natural thing to type once
        # the window accepts a pasted URL, so the CLI should not be pickier.
        # Whole thing first. Splitting on "@" up front used to come first and
        # was wrong for an SSH URL, which contains one: `git@github.com:o/r`
        # split into repo "git" and branch "github.com:o/r".
        found = parse_repo(rest)
        if found is not None:
            repo, branch, folder = found
        else:
            spec, branch, folder = split_repo_spec(rest)
            found = parse_repo(spec)
            if found is None:
                account = github_account(spec)
                if account:
                    die(f"'{account}' is a GitHub account, not a repository -- it may hold\n"
                        "     many addons. Open the one you want on github.com and paste\n"
                        f"     that address, or write it as {account}/repo-name.")
                die(f"cannot see a GitHub repository in '{spec}'.\n"
                    "     Expected owner/repo, or a github.com URL.")
            repo, url_branch, url_folder = found
            branch = branch or url_branch
            folder = folder or url_folder
        normalised = f"github:{repo}"
        if branch:
            normalised += f"@{branch}"
        if folder:
            normalised += f"#{folder}"
        return normalised, kind, None
    if kind != "local":
        return source, kind, None

    local_path = Path(rest).expanduser().resolve()
    if local_path.is_dir() and find_toc(local_path) is None:
        candidate = local_path / addon
        if find_toc(candidate) is not None:
            local_path = candidate
        else:
            die(f"{local_path} holds no {addon}/{addon}.toc")
    return f"local:{local_path}", kind, local_path


def set_source(
    state: dict, addon: str, source: str, *, copy: bool = False, backup: bool | None = None
) -> tuple[dict, Path | None]:
    """Bind one addon to a source. Returns (entry, local_path)."""
    entries = one_install(state, "set_source").setdefault("addons", {})
    source, kind, local_path = resolve_source(addon, source)

    entry = entries.setdefault(addon, new_entry(addon))
    entry["source"] = source
    entry["mode"] = "copy" if copy else entry.get("mode", "link")
    if backup is not None:
        entry["backup"] = bool(backup)
    entry.pop("suggested", None)
    return entry, local_path


def accept_suggestions(state: dict) -> list[tuple[str, str]]:
    """Take every source `scan` suggested. Returns the (name, source) pairs."""
    taken = []
    for name, entry in sorted(one_install(state, "accept_suggestions").get("addons", {}).items()):
        if entry.get("source") == "unmanaged" and entry.get("suggested"):
            entry["source"] = entry.pop("suggested")
            taken.append((name, entry["source"]))
    return taken


def display_order(entries: dict) -> list[str]:
    """Addon names: the ones with a source first, then the rest, each A-Z.

    Both front ends list every addon in the AddOns folder, and on a real
    install most of them are unmanaged -- installed by hand, or left over. Pure
    alphabetical order scatters the handful this tool actually maintains
    through fifty rows it does not, so the list you came to read is the one you
    have to hunt for. Bound addons are what the window is for; they go first.

    Within each group the order is still alphabetical, because that is the only
    order somebody can predict when looking for one name.
    """
    def where(name: str) -> tuple[bool, str]:
        source = entries.get(name, {}).get("source", "unmanaged")
        return (source == "unmanaged", name.lower())

    return sorted(entries, key=where)


def github_message(exc: urllib.error.HTTPError) -> str:
    """GitHub explains itself in the response body; surface that, not the code."""
    try:
        return json.loads(exc.read().decode()).get("message", "no detail given")
    except Exception:
        return "no detail given"


# ── the GitHub token ─────────────────────────────────────────────────────────
#
# One token does two jobs, and only the second one is new:
#
#   raises the rate limit   60 calls an hour becomes 5000. Optional, and the
#                           pacing above is what makes it optional.
#   opens a private repo    without one, a private repository is indistinguish-
#                           able from a repository that does not exist -- which
#                           is exactly what GitHub tells an anonymous caller,
#                           and it is the whole reason this section exists.
#
# Three places are asked, in order, and the first answer wins:
#
#   GITHUB_TOKEN            the environment. Read fresh every time, never
#                           stored, and it beats everything below so that a
#                           one-off `GITHUB_TOKEN=... addons.py update` works
#                           without disturbing what is saved.
#   the OS secret store     what the window's sign-in button writes.
#   git's credential helper what the machine already knows. A developer with a
#                           private addon repo has almost certainly already
#                           told Git Credential Manager or `gh auth login`
#                           about it, and asking them to paste a token they
#                           have already pasted once is a poor greeting.
#
# The last two are looked up once per process and remembered, because both
# shell out and neither answer changes mid-run.

TOKEN_SERVICE = "wow-addons-from-github"
TOKEN_ACCOUNT = "github"

_UNASKED = object()

_token_found: object = _UNASKED
"""The non-environment answer, looked up at most once. _UNASKED until asked."""

_token_lock = threading.Lock()
_token_generation = 0
"""Bumped by every invalidation, so a lookup that started before one can tell.

The rest of this module assumes a single worker and needs no locking. This does
not get that assumption: the window resolves the token off its own thread so
that a slow keyring cannot freeze the UI, which puts two threads on this cache
at once. Without the counter, a lookup that began before a sign-in can finish
after it and write its "nobody is signed in" answer over the top -- and the
next download goes out anonymous, moments after somebody signed in.
"""


def one_line(value: str | None) -> str | None:
    """The first non-empty line of a token, or None. Every token passes through here.

    A token is one line by definition, and this is the last point before it
    becomes an `Authorization:` header. Python refuses a header value with a
    bare newline in it -- but a newline followed by a space or tab is legal
    header folding and is NOT refused, so a two-line token would append
    whatever came after it to the request as another header.

    Reaching that needs write access to the user's own keyring or 0600 file,
    by which point the token is already gone, so this is tidiness rather than a
    hole being closed. It costs one line, and a stray trailing line in that
    file is a much likelier way to arrive here than an attacker.
    """
    for line in (value or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def token_path() -> Path:
    """The fallback store: beside the manifest, readable only by its owner.

    Used when no OS secret store answers -- a headless Linux box with no
    keyring daemon is the ordinary case, not an exotic one.
    """
    return CONFIG_DIR / "github-token"


def _run_quiet(argv: list[str], feed: str | None = None) -> tuple[int, str]:
    """Run a helper and return (code, stdout). Never raises, never prints.

    Every caller below treats any failure as "this store has no answer", which
    is the correct reading of a missing binary, an absent keyring daemon, a
    locked keychain and a user who cancelled the unlock prompt alike.
    """
    try:
        result = subprocess.run(
            argv,
            input=feed,
            capture_output=True,
            text=True,
            timeout=20,
            # Windows would otherwise flash a console window for each of these,
            # from a GUI that has none of its own.
            **({"creationflags": 0x08000000} if on_windows() else {}),
        )
    except Exception:
        return 1, ""
    return result.returncode, result.stdout


# -- the OS secret store -----------------------------------------------------
#
# Three platforms, three mechanisms, one three-function interface. Each returns
# None or False to mean "not available here", and every caller falls through to
# the 0600 file rather than failing.


def _powershell(script: str, feed: str | None = None) -> str | None:
    code, out = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], feed
    )
    return out.strip() if code == 0 and out.strip() else None


def _dpapi_path() -> Path:
    return CONFIG_DIR / "github-token.dpapi"


def secret_store_name() -> str:
    """What this platform's secret store is called, for a sentence about it.

    Windows has no keyring: the token is sealed with DPAPI against the Windows
    account. Calling that "your keyring" in the one line somebody reads to find
    out where their token went is wrong on the platform most likely to be
    running this.
    """
    if on_windows():
        return "Windows, encrypted against your account"
    if sys.platform == "darwin":
        return "your login keychain"
    return "your system keyring"


def secret_get() -> str | None:
    """The token the OS is holding for us, or None if it is not holding one."""
    if on_windows():
        # DPAPI rather than the Credential Manager: ConvertTo-SecureString is
        # in stock PowerShell, and the credential cmdlets are not -- they need
        # a module from the gallery, which is not a thing to ask of somebody
        # who wants to update an addon. The ciphertext is keyed to this Windows
        # account, so the file below is useless if copied off the machine.
        blob = _dpapi_path()
        if not blob.exists():
            return None
        return _powershell(
            "$e = [Console]::In.ReadToEnd().Trim();"
            "$s = ConvertTo-SecureString $e;"
            "[Runtime.InteropServices.Marshal]::PtrToStringAuto("
            "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))",
            _read_quietly(blob),
        )
    if sys.platform == "darwin":
        code, out = _run_quiet(
            ["security", "find-generic-password",
             "-s", TOKEN_SERVICE, "-a", TOKEN_ACCOUNT, "-w"]
        )
        return out.strip() if code == 0 and out.strip() else None
    code, out = _run_quiet(
        ["secret-tool", "lookup", "service", TOKEN_SERVICE, "account", TOKEN_ACCOUNT]
    )
    return out.strip() if code == 0 and out.strip() else None


def secret_set(token: str) -> bool:
    """Hand the token to the OS. False if this machine has nowhere to put it."""
    if on_windows():
        sealed = _powershell(
            "$p = [Console]::In.ReadToEnd().Trim();"
            "ConvertTo-SecureString $p -AsPlainText -Force | ConvertFrom-SecureString",
            token,
        )
        if not sealed:
            return False
        return _write_privately(_dpapi_path(), sealed)
    if sys.platform == "darwin":
        # -U so that signing in twice replaces the entry rather than failing on
        # the second attempt. The token is on the command line here because
        # `security` offers no way to feed it in; macOS shows another user's
        # argv only to root, and the process lives for milliseconds.
        code, _ = _run_quiet(
            ["security", "add-generic-password", "-U",
             "-s", TOKEN_SERVICE, "-a", TOKEN_ACCOUNT,
             "-l", "WoW Addons from GitHub", "-w", token]
        )
        return code == 0
    # secret-tool takes the secret on stdin, which is the one of the three that
    # never appears in a process listing.
    code, _ = _run_quiet(
        ["secret-tool", "store", "--label=WoW Addons from GitHub",
         "service", TOKEN_SERVICE, "account", TOKEN_ACCOUNT],
        token,
    )
    return code == 0


def secret_clear() -> None:
    """Forget it. Best effort: signing out must not fail because a store did."""
    if on_windows():
        _dpapi_path().unlink(missing_ok=True)
        return
    if sys.platform == "darwin":
        _run_quiet(["security", "delete-generic-password",
                    "-s", TOKEN_SERVICE, "-a", TOKEN_ACCOUNT])
        return
    _run_quiet(["secret-tool", "clear",
                "service", TOKEN_SERVICE, "account", TOKEN_ACCOUNT])


# -- the 0600 file, for when there is no secret store ------------------------


def _read_quietly(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except Exception:
        return None


def _write_privately(path: Path, text: str) -> bool:
    """Write owner-only, and be owner-only from the moment the file exists.

    Creating it and chmod-ing afterwards leaves a window in which the token is
    world-readable. Opening with mode 0600 has no such window; on Windows the
    mode is ignored, which is why the payload there is DPAPI ciphertext rather
    than the token.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as out:
            out.write(text + "\n")
        return True
    except Exception:
        return False


def stored_token() -> str | None:
    """What sign-in saved, wherever it managed to save it."""
    return secret_get() or _read_quietly(token_path())


def stored_where() -> str | None:
    """Which of the two saved it, for the window to say so. None if neither did."""
    if secret_get():
        return "keyring"
    if _read_quietly(token_path()):
        return "file"
    return None


def save_token(token: str) -> str:
    """Keep this token for next time. Returns where it went, for the caller to say.

    The plain file is not a silent consolation prize -- the window tells the
    person which of the two happened, because "in your keyring" and "in a file
    only you can read" are different promises and they are entitled to know
    which one they got.
    """
    forget_cached_token()
    token = one_line(token) or ""
    if secret_set(token):
        # Nothing should be left in the weaker store once the stronger one has
        # it, or signing in on a machine that gains a keyring would leave the
        # old copy behind for ever.
        token_path().unlink(missing_ok=True)
        return "keyring"
    if _write_privately(token_path(), token):
        return "file"
    die(f"could not save the token: neither the system keyring nor {tilde(str(token_path()))} would take it")


def forget_token() -> None:
    """Sign out: remove it from both stores."""
    forget_cached_token()
    secret_clear()
    token_path().unlink(missing_ok=True)


# -- what the machine already knows ------------------------------------------


def credential_token() -> str | None:
    """A token Git or the GitHub CLI is already holding for github.com.

    Read-only and non-interactive on purpose. `git credential fill` will
    happily open a browser or a prompt if no helper can answer, which from a
    window that has not asked for anything would be a mystery; GIT_TERMINAL_
    PROMPT=0 turns that into a plain failure, and a plain failure here just
    means "nothing saved", which is the truth.
    """
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=20, env=environment,
            **({"creationflags": 0x08000000} if on_windows() else {}),
        )
    except Exception:
        result = None
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key == "password" and value.strip():
                return value.strip()

    code, out = _run_quiet(["gh", "auth", "token"])
    if code == 0 and out.strip():
        return out.strip()
    return None


# -- what everything above is for --------------------------------------------


def resolve_token() -> tuple[str | None, str | None]:
    """(token, where it came from) for the saved and inherited sources.

    Cached, and deliberately not cheap to call the first time: two of the three
    answers come from a subprocess. Both callers below went through their own
    copy of this at first, which meant the window asked `git credential fill`
    the same question on every keystroke in the token box -- a subprocess per
    character, on the thread drawing the box.
    """
    global _token_found
    while True:
        with _token_lock:
            if _token_found is not _UNASKED:
                return _token_found  # type: ignore[return-value]
            began_at = _token_generation

        # Outside the lock: these are subprocesses, and holding a lock across
        # one would hand a slow keyring the power to block whatever else is
        # asking -- which is the freeze this was threaded to avoid.
        found = _look_up_token()

        with _token_lock:
            if began_at == _token_generation:
                _token_found = found
                return found
        # Signed in or out while we were looking, so `found` describes the
        # state before that and must not be cached or returned. Ask again.


def _look_up_token() -> tuple[str | None, str | None]:
    """The actual lookup, with no caching: the saved copy, else what Git has."""
    saved = stored_token()
    if saved:
        # `or "file"` covers the one gap between the two calls: a keyring that
        # answered for `stored_token` and then would not say where.
        return saved, stored_where() or "file"
    inherited = credential_token()
    return (inherited, "git") if inherited else (None, None)


def github_token() -> str | None:
    """The token to send, or None to go anonymous.

    Every request in this module goes through here rather than reading the
    environment directly, so that "where does the token come from" is answered
    in one place and adding a fourth source later is one edit.
    """
    from_environment = one_line(os.environ.get("GITHUB_TOKEN"))
    if from_environment:
        return from_environment
    return one_line(resolve_token()[0])


def forget_cached_token() -> None:
    """Ask the stores again next time. Called whenever the answer may have moved."""
    global _token_found, _token_generation
    with _token_lock:
        _token_found = _UNASKED
        _token_generation += 1


def token_source() -> str | None:
    """Which of the sources answered, for the window to show. None if none did.

    Deliberately does not return the token: a status line has no business
    holding one, and this is called to draw a label.
    """
    if one_line(os.environ.get("GITHUB_TOKEN")):
        return "GITHUB_TOKEN"
    return resolve_token()[1]


def token_identity(token: str) -> str:
    """Whose token is this? Raises Fail with GitHub's own words if it is not valid.

    The point of the Test button: a token that is merely well-formed proves
    nothing, and the failure people actually hit -- a fine-grained token that
    was never granted this repository -- looks exactly like success until an
    install fails much later.
    """
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {one_line(token)}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode()).get("login") or "(unnamed)"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            die("GitHub does not recognise that token. Check it was copied whole, and has not expired.")
        die(f"GitHub refused the token ({exc.code}): {github_message(exc)}")
    except urllib.error.URLError as exc:
        die(f"could not reach GitHub: {exc.reason}")



# -- pacing GitHub -----------------------------------------------------------
#
# GitHub allows 60 API calls an hour without a token and 5000 with one, and
# separately objects to bursts however much quota is left. One addon costs one
# to three calls, so "Update all" over thirty of them is a burst of eighty
# requests fired as fast as the network answers -- the exact shape that trips
# both limits, and the reason "GitHub rate limit reached" was the ordinary
# outcome of a normal-sized addon list rather than a rare one.
#
# Three things happen below, in order of how much they help:
#
#   an answer is reused        two addons out of the same repository asked the
#                              same question; the second need not cost a call
#   calls are spaced out       always a little, and much more once the quota is
#                              nearly gone, so the last few last as long as
#                              they can rather than evaporating in one second
#   the wall is not re-hit     once the quota is known to be spent, the rest of
#                              the run fails immediately from what we already
#                              know instead of spending thirty more round trips
#                              to be told the same thing thirty more times
#
# None of it invents quota. With sixty calls an hour and eighty needed, some of
# the run will still fail; what changes is that it fails once, quickly, saying
# when it can be retried -- and that the ordinary list of a dozen addons, which
# fits comfortably inside the limit, stops being refused for burst alone.

GITHUB_MIN_GAP = 0.25
"""Seconds between two API calls, always. Cheap, and burst limits are real."""

GITHUB_LOW_WATER = 20
"""Below this many calls left, stop spending them at full speed."""

GITHUB_MAX_GAP = 5.0
"""Never pause longer than this between calls -- past it a run looks hung."""

GITHUB_MAX_WAIT = 60.0
"""Longest we will sit out a limit before giving up and saying so."""

CACHE_SECONDS = 120.0
"""How long an API answer stays reusable. Long enough to cover one run of
`Update all`, short enough that a commit pushed a minute ago is not hidden."""


def header(headers, name: str) -> str | None:
    """One header, case-insensitively, from a real response or a plain dict."""
    if headers is None:
        return None
    value = headers.get(name)
    if value is None:
        try:
            items = list(headers.items())
        except Exception:
            return None
        for key, found in items:
            if key.lower() == name:
                return found
    return value


def header_number(headers, name: str) -> float | None:
    """A numeric header, or None if it is missing or not a number."""
    value = header(headers, name)
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


_wait_hook = _nothing


def set_wait_hook(hook=None) -> None:
    """Who to tell that a request is being held back, and why.

    A front end sets this; the engine still prints nothing. Without it the
    waiting is silent, which for a quarter of a second is right and for a
    minute is indistinguishable from a hang.
    """
    global _wait_hook
    _wait_hook = hook or _nothing


def report_wait(seconds: float, why: str) -> None:
    """Say we are waiting -- but only when it is long enough to be noticed."""
    if seconds >= 1.0:
        _wait_hook(seconds, why)


class Throttle:
    """Spaces out GitHub calls, and remembers when the quota ran out.

    `sleep` and `clock` are injected so a test can exercise the pacing without
    actually sitting there for it.
    """

    def __init__(self, *, sleep=time.sleep, clock=time.time):
        self._sleep = sleep
        self._clock = clock
        self.forget()

    def forget(self) -> None:
        """Back to knowing nothing -- new process, or a test starting over."""
        self.remaining: float | None = None
        self.reset_at: float | None = None
        self._last_call: float | None = None

    def gap(self) -> float:
        """How long to leave between the last call and the next one.

        The floor is there for burst limits. Above it, what is left is spread
        across the time until the quota comes back, so a nearly-spent quota is
        rationed rather than emptied -- capped, because a pause long enough to
        look like a crash is not an improvement on an error message.
        """
        gap = GITHUB_MIN_GAP
        if self.remaining is not None and self.reset_at is not None and self.remaining <= GITHUB_LOW_WATER:
            left = max(self.reset_at - self._clock(), 0.0)
            gap = max(gap, min(left / max(self.remaining, 1.0), GITHUB_MAX_GAP))
        return gap

    def wait_turn(self) -> float:
        """Hold the next call back until its turn. Returns the seconds waited."""
        now = self._clock()
        waited = 0.0
        if self._last_call is not None:
            due = self._last_call + self.gap()
            if due > now:
                waited = due - now
                report_wait(waited, "pacing requests to stay inside GitHub's rate limit")
                self._sleep(waited)
        self._last_call = self._clock()
        return waited

    def observe(self, headers) -> None:
        """Learn from a response how much quota is left and when it returns."""
        remaining = header_number(headers, "x-ratelimit-remaining")
        if remaining is not None:
            self.remaining = remaining
        reset = header_number(headers, "x-ratelimit-reset")
        if reset is not None:
            self.reset_at = reset

    def spent(self) -> bool:
        """True while the quota is known to be gone and not yet back."""
        return self.remaining == 0 and self.reset_at is not None and self.reset_at > self._clock()

    def sit_out(self, headers) -> bool:
        """Wait out a short limit; say whether retrying is now worth it.

        A secondary (burst) limit clears in a minute and is worth waiting for.
        An exhausted hourly quota is not: nobody wants a window that sits there
        for forty minutes pretending to work.
        """
        delay = header_number(headers, "retry-after")
        if delay is None:
            reset = header_number(headers, "x-ratelimit-reset")
            delay = None if reset is None else reset - self._clock()
        if delay is None or delay > GITHUB_MAX_WAIT:
            return False
        delay = max(delay, 1.0)
        report_wait(delay, "GitHub asked for a pause; waiting it out")
        self._sleep(delay)
        self._last_call = self._clock()
        return True


THROTTLE = Throttle()


# -- what we already know ----------------------------------------------------
#
# Two caches, because they answer two different questions.
#
# The run cache is "did we already ask this, a moment ago, in this same pass?"
# Ten addons out of one repository all want its default branch, and a branch
# cannot meaningfully move between two rows of the same run. It lives for one
# run and is thrown away, so a second run never reads a stale answer.
#
# The ETag store is "has this changed since the last time we looked?" GitHub
# stamps every response with an ETag; send it back as If-None-Match and an
# unchanged resource answers 304 with no body -- and, crucially, a 304 is NOT
# billed against the hourly quota. So a check that finds nothing new is free,
# however often it is run. It is also fresher than a timed cache: the answer
# always comes from GitHub, so a commit pushed ten seconds ago shows up.

CACHE_ENTRIES = 400
"""Most URLs kept between runs. A big list is a few dozen; this is slack."""

CACHE_MAX_BODY = 256 * 1024
"""Bodies larger than this are revalidated but not stored, to bound the file."""

_run_cache: dict[str, dict | None] = {}
_store: dict[str, dict] | None = None
_store_dirty = False

_MISS = object()
"""Distinguishes "not asked yet" from an answer of None, which 404 is."""


def cache_path() -> Path:
    """Beside the manifest: same directory, same lifetime, easy to delete."""
    return CONFIG_DIR / "github-cache.json"


def store() -> dict[str, dict]:
    """The ETag store, read from disk once per process.

    A damaged or unreadable file is not worth a word to the user: the only
    thing lost is some free revalidation, and the run works without it.
    """
    global _store
    if _store is None:
        _store = {}
        try:
            loaded = json.loads(cache_path().read_text())
            if isinstance(loaded, dict):
                _store = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except Exception:
            _store = {}
    return _store


def remember(url: str, etag: str | None, body: dict | None) -> None:
    """Keep an answer and the tag that will tell us whether it still holds."""
    global _store_dirty
    if not etag:
        return
    try:
        if len(json.dumps(body)) > CACHE_MAX_BODY:
            return
    except Exception:
        return
    store()[url] = {"etag": etag, "body": body, "at": time.time()}
    _store_dirty = True


def touch(url: str) -> None:
    """Mark an entry as still in use, so pruning keeps what is being used."""
    global _store_dirty
    entry = store().get(url)
    if entry is not None:
        entry["at"] = time.time()
        _store_dirty = True


def begin_run() -> None:
    """Starting a fresh pass: forget what was asked during the last one.

    This is what keeps `Update` honest straight after a push. Nothing is
    served from a timer -- every run revalidates, and pays nothing for the
    answers that have not changed.
    """
    _run_cache.clear()
    _recent_archive.clear()
    # Refetched every run on purpose: comparing this run's listing against the
    # one kept from last run is what says whether anything moved, and a
    # listing held over from the previous pass would answer "no" for ever.
    _refs_now.clear()
    _refs_before.clear()


def end_run() -> None:
    """Write the ETag store, if anything was learned. Never raises."""
    global _store_dirty
    if not _store_dirty or _store is None:
        return
    keep = sorted(_store.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)
    pruned = dict(keep[:CACHE_ENTRIES])
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        cache_path().write_text(json.dumps(pruned))
        _store.clear()
        _store.update(pruned)
        _store_dirty = False
    except OSError:
        # An unwritable config directory costs free revalidation next time and
        # nothing else. Not worth failing an otherwise good run over.
        pass


def forget_github_state() -> None:
    """Drop everything remembered about GitHub, in memory. For a fresh look."""
    global _store, _store_dirty
    _run_cache.clear()
    _recent_archive.clear()
    _refs_now.clear()
    _refs_before.clear()
    _store = None
    _store_dirty = False
    THROTTLE.forget()


def quota_left() -> int | None:
    """GitHub calls left this hour, as of the last response. None until asked.

    Worth showing: the number is the whole reason a run fails, and seeing it
    fall is how somebody learns that pinning a branch or binding a folder
    locally is cheaper than not.
    """
    return None if THROTTLE.remaining is None else int(THROTTLE.remaining)


def rate_limit_message(reset_at: float | None) -> str:
    when = ""
    if reset_at:
        when = " until " + datetime.datetime.fromtimestamp(reset_at).strftime("%H:%M")
    return (
        f"GitHub rate limit reached{when}."
        "\n     Set GITHUB_TOKEN to a read-only token to raise it, or wait."
    )


def is_rate_limit(headers, message: str) -> bool:
    """Whether this 403/429 is a limit rather than permissions or a proxy.

    A 403 is NOT automatically a rate limit -- a private repo, a blocked egress
    proxy and an exhausted quota all land here, and telling someone to "wait an
    hour" when the real cause is a proxy wastes their evening. The remaining
    header distinguishes the hourly quota; the body names the burst limit,
    which arrives with quota still on the clock.
    """
    if header_number(headers, "x-ratelimit-remaining") == 0:
        return True
    lowered = message.lower()
    return "secondary rate limit" in lowered or "abuse detection" in lowered


def http_json(url: str) -> dict | None:
    """One GitHub API call -- asked once per run, and free when nothing changed.

    The ETag round trip still happens; what it does not do is cost quota. That
    is the difference between "you may check twice an hour" and "check as often
    as you like, as long as your addons are not moving".
    """
    memo = _run_cache.get(url, _MISS)
    if memo is not _MISS:
        return memo

    known = store().get(url)
    conditional = bool(known and known.get("etag") and "body" in known)

    for attempt in (1, 2):
        if THROTTLE.spent():
            # Nothing to gain from asking: the answer is already known, and
            # asking anyway is what turns one exhausted quota into thirty
            # identical failures and a run that takes a minute to say so.
            die(rate_limit_message(THROTTLE.reset_at))

        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        )
        token = github_token()
        if token:
            # Optional for a public repository -- unauthenticated is 60
            # requests/hour, which a personal addon list can still reach on a
            # big update. Required for a private one, which is invisible
            # without it. See `github_token` for where it comes from.
            request.add_header("Authorization", f"Bearer {token}")
        if conditional:
            request.add_header("If-None-Match", known["etag"])

        THROTTLE.wait_turn()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                THROTTLE.observe(response.headers)
                answer = json.loads(response.read().decode())
                remember(url, header(response.headers, "etag"), answer)
                _run_cache[url] = answer
                return answer
        except urllib.error.HTTPError as exc:
            THROTTLE.observe(exc.headers)
            if exc.code == 304:
                # Unchanged, and not billed. The stored body is the answer.
                touch(url)
                _run_cache[url] = known["body"]
                return known["body"]
            if exc.code == 404:
                # Remembered for this run only: a repository that publishes no
                # releases today may publish one tomorrow, and a 404 carries no
                # ETag to revalidate against.
                _run_cache[url] = None
                return None
            if exc.code in (403, 429):
                message = github_message(exc)
                if is_rate_limit(exc.headers, message):
                    if attempt == 1 and THROTTLE.sit_out(exc.headers):
                        continue
                    die(rate_limit_message(THROTTLE.reset_at))
                die(f"GitHub refused the request ({exc.code}): {message}")
            if exc.code == 401:
                die("GitHub rejected GITHUB_TOKEN (401). Unset it, or use a valid read-only token.")
            die(f"GitHub returned {exc.code} for {url}: {github_message(exc)}")
        except urllib.error.URLError as exc:
            die(f"could not reach GitHub: {exc.reason}")

    # Unreachable: every branch above returns or raises, and only the first
    # attempt may retry. Here so that a future edit which breaks that cannot
    # quietly return None, which every caller would read as "404, no such thing".
    die(f"gave up talking to GitHub for {url}")


def split_repo_spec(repo_spec: str) -> tuple[str, str | None, str | None]:
    """'owner/repo@branch#Folder' -> ('owner/repo', 'branch', 'Folder').

    The folder is split off first: a branch name may not contain '#', but a
    path certainly may contain '@' (and nothing stops a branch containing '/'),
    so taking them in the other order would mis-split both.

    Several folders may be named, separated by commas: an addon that is really
    two folders of the same repository -- a main addon and its companion -- is
    one thing to the person updating it, and should be one row in the table.
    `wanted_folders` splits them; this returns the field as written so the
    source string round-trips unchanged.
    """
    folder = None
    if "#" in repo_spec:
        repo_spec, folder = repo_spec.split("#", 1)
        folder = folder.strip("/") or None
    branch = None
    if "@" in repo_spec:
        repo_spec, branch = repo_spec.split("@", 1)
    return repo_spec, branch, folder


def names_a_toc(pick: str) -> bool:
    """Does this choice name a .toc at the repository root rather than a folder?

    Some repositories ARE the addon and ship several .toc files side by side,
    one per client -- NotPlater is NotPlater-2.4.3.toc and NotPlater-3.3.5.toc
    in one root, and the folder you unzip it into has to be named after the one
    you want, because the client loads <Folder>/<Folder>.toc and nothing else.

    There is no folder to name in that repository, so the source names the .toc
    instead: `github:RichSteini/NotPlater#NotPlater-3.3.5.toc`. The suffix is
    what tells the two apart, everywhere, and it cannot collide with a real
    choice: a directory called `X.toc` is not a thing anybody ships.
    """
    return pick.lower().endswith(".toc")


def addon_name_for(pick: str) -> str:
    """The AddOns folder a chosen folder-or-.toc installs as."""
    leaf = pick.rsplit("/", 1)[-1]
    return leaf[:-len(".toc")] if names_a_toc(leaf) else leaf


def wanted_folders(folder: str | None) -> list[str]:
    """The folders a source names, in order, with blanks and slashes trimmed."""
    if not folder:
        return []
    return [part.strip().strip("/") for part in folder.split(",") if part.strip().strip("/")]


# -- asking git instead of the API -------------------------------------------
#
# Every question this tool asks about a repository used to go to the REST API,
# which allows 60 calls an hour without a token. Most of them do not need to.
#
# Before a clone, git asks the server to list what it has, at
#
#     https://github.com/owner/repo.git/info/refs?service=git-upload-pack
#
# That is the git wire protocol, not the REST API: it is served from github.com,
# it carries no x-ratelimit headers, and it is not billed against the hourly
# quota -- it is the request every `git fetch` in the world begins with. One of
# them yields, for a whole repository at once:
#
#     the default branch      advertised as symref=HEAD:refs/heads/<name>
#     every branch, with the commit it points at
#     every tag, with the commit it points at
#
# Which answers outright the two questions that cost the most calls, and rules
# out a third: a published release always has a tag, so a repository with no
# tags cannot have a release, and the release endpoint need not be asked at all.
#
# What it cannot answer is history -- "which commit last touched this folder" is
# not in a ref listing. But a folder cannot change unless the branch containing
# it moves, so comparing this listing against the last one tells us when that
# question can be skipped rather than asked.
#
# Everything here fails soft. A repository that is private, or a network that
# blocks this, simply falls back to the REST path, exactly as a codeload archive
# falls back to the API zipball.

GIT_REFS_URL = "https://github.com/{repo}.git/info/refs?service=git-upload-pack"

REFS_MAX_BYTES = 4 * 1024 * 1024
"""A busy repository advertises a lot of refs; stop reading well before silly."""

_refs_now: dict[str, dict | None] = {}
_refs_before: dict[str, dict | None] = {}


def pkt_lines(raw: bytes):
    """Split a git pkt-line stream: four hex length bytes, then that many bytes.

    A length of 0000 is a section terminator and carries no payload. Anything
    malformed ends the walk rather than raising -- this is a best-effort read of
    an optional shortcut, not a parser anybody depends on being strict.
    """
    at = 0
    while at + 4 <= len(raw):
        try:
            size = int(raw[at:at + 4], 16)
        except ValueError:
            return
        if size == 0:
            at += 4
            continue
        if size < 4 or at + size > len(raw):
            return
        yield raw[at + 4:at + size]
        at += size


def read_advertisement(raw: bytes) -> dict:
    """The refs a server advertised, as {'head': branch, 'refs': {name: sha}}.

    Only branches and tags are kept. GitHub also advertises every pull request
    as refs/pull/N/head, which on a busy repository is most of the response and
    none of the answer. Peeled tags (refs/tags/x^{}) name the commit a tag
    object points at; the tag's own entry is the one that matches a release.
    """
    head, refs = None, {}
    for line in pkt_lines(raw):
        text = line.decode("utf-8", "replace").strip()
        if not text or text.startswith("#"):
            continue
        payload, _, capabilities = text.partition("\x00")
        for capability in capabilities.split():
            if capability.startswith("symref=HEAD:refs/heads/"):
                head = capability.split("refs/heads/", 1)[1]
        sha, _, name = payload.strip().partition(" ")
        if name.endswith("^{}"):
            continue
        if name.startswith(("refs/heads/", "refs/tags/")):
            refs[name] = sha
    return {"head": head, "refs": refs}


def git_refs(repo: str) -> dict | None:
    """What the repository advertises, asked once per run. None if unavailable.

    Also remembers the previous run's answer before overwriting it, because
    "did anything move since last time" is the question that lets the expensive
    calls be skipped.
    """
    if repo in _refs_now:
        return _refs_now[repo]

    _refs_now[repo] = None
    request = urllib.request.Request(
        GIT_REFS_URL.format(repo=repo),
        # git's own User-Agent: this is the git protocol, and a server may
        # reasonably treat it differently from a browser.
        headers={"User-Agent": "git/2.40 (wow-addons-sync)"},
    )
    token = github_token()
    if token:
        # Basic, not Bearer: the git transport authenticates the way git does,
        # which is what makes a private repository work here at all.
        import base64

        pair = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        request.add_header("Authorization", f"Basic {pair}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            found = read_advertisement(response.read(REFS_MAX_BYTES))
    except Exception:
        # Private, blocked, no_api, or something new: the REST path still
        # works and is what every caller falls back to.
        return None
    if not found["refs"]:
        return None

    _refs_before[repo] = store().get(f"refs:{repo}")
    remember_refs(repo, found)
    _refs_now[repo] = found
    return found


def remember_refs(repo: str, found: dict) -> None:
    """Keep this listing, so the next run can see what moved."""
    global _store_dirty
    store()[f"refs:{repo}"] = {"refs": found["refs"], "head": found["head"], "at": time.time()}
    _store_dirty = True


def ref_sha(repo: str, ref: str) -> str | None:
    """The commit a branch or tag points at, without spending a call."""
    found = git_refs(repo)
    if not found:
        return None
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if name in found["refs"]:
            return found["refs"][name]
    return None


def names_a_tag(repo: str, ref: str) -> bool:
    """Whether `ref` is a tag rather than a branch, per the advertisement."""
    found = git_refs(repo)
    if not found:
        return False
    return (f"refs/tags/{ref}" in found["refs"]
            and f"refs/heads/{ref}" not in found["refs"])


def ref_moved(repo: str, ref: str) -> bool:
    """Whether `ref` points somewhere new since the last run.

    True whenever we cannot tell -- an unknown answer must never be reported as
    "nothing changed", because the cost of that mistake is an addon that
    silently stops updating.
    """
    now = ref_sha(repo, ref)
    if now is None:
        return True
    before = (_refs_before.get(repo) or {}).get("refs", {})
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if name in before:
            return before[name] != now
    return True


def repo_has_tags(repo: str) -> bool | None:
    """Whether the repository has any tags. None when we could not find out.

    A published release always has a tag behind it, so "no tags" is a complete
    answer to "is there a release" -- and a free one.
    """
    found = git_refs(repo)
    if not found:
        return None
    return any(name.startswith("refs/tags/") for name in found["refs"])


def default_branch(repo: str) -> str:
    """Which branch this repository calls its main one.

    Advertised by git, so this normally costs nothing. The REST call is the
    fallback -- and still the thing that reports a private or missing
    repository properly, which is why it is not skipped when the shortcut
    fails.
    """
    found = git_refs(repo)
    if found and found.get("head"):
        return found["head"]
    info = http_json(f"https://api.github.com/repos/{repo}")
    if not info:
        die(unreadable_repo(repo))
    return info.get("default_branch", "master")


def unreadable_repo(repo: str) -> str:
    """What to say when GitHub answers 404 for a repository.

    GitHub does not distinguish "no such repository" from "you may not see this
    one" -- telling an anonymous caller which would leak the existence of every
    private repo on the site. So both arrive here as the same 404, and the
    message has to cover both without guessing.

    What it CAN do is tell them apart on the one axis that matters: with no
    token, "private" is a live possibility and the fix is to sign in; with a
    token that GitHub accepted, the repository is either gone or outside what
    that token was granted -- and a fine-grained token that simply was not
    given this repository is the likeliest way to arrive here twice.
    """
    if github_token():
        return (f"cannot see {repo}. Either it does not exist, or the GitHub token in use "
                "was not granted access to it -- a fine-grained token has to list the "
                "repository explicitly.")
    return (f"cannot see {repo}. Either it does not exist, or it is private -- "
            "private repositories need a GitHub token (Sign in… in the window, "
            "or set GITHUB_TOKEN).")


def latest_folder_commit(repo: str, branch: str, folder: str) -> str:
    """The last commit that touched one folder -- this addon's real version.

    A repository holding nine addons has one commit history, so its HEAD moves
    whenever ANY of them changes. Versioning an addon by the repo would report
    an update for all nine every time one is touched, and "update available" that
    is usually wrong is worse than no column at all: people stop reading it.

    GitHub answers the narrower question directly, and for the same one request.
    """
    query = urllib.parse.urlencode({"sha": branch, "path": folder, "per_page": 1})
    url = f"https://api.github.com/repos/{repo}/commits?{query}"

    if not ref_moved(repo, branch):
        # The branch this folder lives on points exactly where it did last run,
        # so nothing under it can have changed and the answer we were given
        # then is still the answer. This is the one that matters for a
        # monorepo: ten folders on an unmoved branch used to be ten calls.
        remembered = store().get(url)
        try:
            settled = remembered["body"][0]["sha"][:12]
        except (TypeError, KeyError, IndexError):
            settled = None
        if settled:
            touch(url)
            return settled

    commits = http_json(url)
    if not commits:
        die(f"nothing in {repo} touches '{folder}' -- check the folder name")
    return commits[0]["sha"][:12]


def archive_url(repo: str, ref: str, *, tag: bool = False) -> str:
    """Where to fetch a whole ref as a zip, off the API's meter.

    `api.github.com/repos/o/r/zipball/ref` is a REST call and is billed as one,
    which made the download half of an update as expensive as the checking
    half. codeload is the host behind the green "Download ZIP" button: it is
    not the REST API, does not spend the hourly quota, and is limited far more
    liberally. It is not unlimited and it is not documented, which is why
    `download` keeps the REST URL as a fallback rather than trusting this
    outright.
    """
    kind = "tags" if tag else "heads"
    return f"https://codeload.github.com/{repo}/zip/refs/{kind}/{urllib.parse.quote(ref)}"


def asset_url(asset: dict) -> str:
    """Where to fetch a release asset from: the free host, or the API.

    `browser_download_url` is the green link on the releases page. It is served
    from github.com, costs no quota, and is the right answer -- for a PUBLIC
    repository. For a private one it is a 404 to anybody without a session,
    and no token in a header changes that: it is not an authenticated endpoint.

    The API knows the same asset by an id, will serve the bytes for an
    `Accept: application/octet-stream`, and honours a token. That costs one
    call, which is why it is not simply always used. Having a token at all is
    the signal: it means either a private repository, where this is the only
    path that works, or a public one with 5000 calls an hour to spend, where
    one of them is not worth a branch that only breaks in the private case.
    """
    if github_token() and asset.get("url"):
        return asset["url"]
    return asset["browser_download_url"]


def rest_archive_url(url: str) -> str | None:
    """The REST equivalent of a codeload URL, for when codeload will not serve.

    Costs a call, which is the whole thing we are avoiding -- but a run that
    installs the addon and spends a call beats a run that fails for free.
    """
    prefix = "https://codeload.github.com/"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    owner, _, rest = rest.partition("/")
    repo, _, rest = rest.partition("/")
    if not (owner and repo and rest.startswith("zip/")):
        return None
    ref = rest[len("zip/"):]
    for lead in ("refs/heads/", "refs/tags/"):
        if ref.startswith(lead):
            ref = ref[len(lead):]
            break
    return f"https://api.github.com/repos/{owner}/{repo}/zipball/{ref}"


def latest_release(repo: str) -> dict | None:
    """The newest published release, asked so that "none" costs nothing twice.

    `/releases/latest` is the endpoint that knows the rule -- newest release
    that is neither a draft nor a pre-release -- and for a repository that has
    never published one it answers 404. That is the correct answer, and it is
    the one answer this tool cannot cache: a 404 carries no ETag, so every
    check paid for it again. Six addons bound to repositories without releases
    cost six calls an hour, for ever, to be told six times what had not changed.

    Listing instead answers 200 with an empty array, which does carry an ETag,
    so the second check is free -- and the first entry of the list is the newest
    release, which is the answer outright in the common case. Only when that
    newest one is a draft or a pre-release is the narrower endpoint needed, and
    that answer revalidates for free too.

    No timer, and nothing taken on trust: a release published a minute ago is
    still seen on the next check.
    """
    if repo_has_tags(repo) is False:
        # A published release always has a tag behind it. No tags is therefore a
        # complete answer, and git gave it to us for free -- this is the whole
        # of the cost for an addon bound to a repository that cuts no releases.
        return None

    listed = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=1")
    if not listed:
        # Either no releases (200 and an empty list, free from here on) or no
        # such repo (404). Both mean there is no release to install, and the
        # caller falls back to the branch head; a missing repo is reported by
        # `default_branch` a moment later, which words it properly.
        return None
    newest = listed[0]
    if not newest.get("draft") and not newest.get("prerelease"):
        return newest
    # The newest is one this tool should not install. Which of the older ones
    # counts as current is exactly the question /releases/latest exists to
    # answer, so ask it -- and its answer, 200 or 404, is now the uncommon case.
    return http_json(f"https://api.github.com/repos/{repo}/releases/latest")


def latest_github(repo_spec: str) -> tuple[str, str]:
    """(version, zip url) for the newest thing at owner/repo[@branch][#folder]."""
    repo, branch, folder = split_repo_spec(repo_spec)

    # A .toc pick names no folder -- the repository root is the addon -- so it
    # keeps the repository's own version, releases and all.
    chosen = [pick for pick in wanted_folders(folder) if not names_a_toc(pick)]
    if chosen:
        # A named folder overrides releases deliberately. A release asset is
        # packaged for one addon; there is no reason its contents line up with
        # a path in the source tree, so honouring both would mean guessing.
        ref = branch or default_branch(repo)
        version = latest_folder_commit(repo, ref, chosen[0])
        for extra in chosen[1:]:
            # One version for the row, and it has to move when ANY of the
            # folders does -- otherwise ticking a second folder would quietly
            # stop that folder ever reporting an update.
            version = f"{version}+{latest_folder_commit(repo, ref, extra)[:7]}"
        return version, archive_url(repo, ref)

    if branch:
        advertised = ref_sha(repo, branch)
        if advertised:
            # `@something` accepts a tag as readily as a branch, and the two
            # live at different archive paths. Guessing wrong costs a refused
            # request and a fall back to the REST zipball; the advertisement
            # already says which it is, so there is no need to guess.
            return advertised[:12], archive_url(repo, branch, tag=names_a_tag(repo, branch))
        commits = http_json(f"https://api.github.com/repos/{repo}/commits/{branch}")
        if not commits:
            die(f"no branch '{branch}' in {repo}")
        return commits["sha"][:12], archive_url(repo, branch)

    release = latest_release(repo)
    if release:
        # Prefer an attached .zip: that is the packaged addon, laid out the way
        # it should sit in AddOns. The source archive is the fallback and needs
        # its wrapper directory stripped, which install_zip handles.
        for asset in release.get("assets", []):
            if asset["name"].lower().endswith(".zip"):
                return release["tag_name"], asset_url(asset)
        return release["tag_name"], archive_url(repo, release["tag_name"], tag=True)

    ref = default_branch(repo)
    advertised = ref_sha(repo, ref)
    if advertised:
        return advertised[:12], archive_url(repo, ref)
    commits = http_json(f"https://api.github.com/repos/{repo}/commits/{ref}")
    sha = commits["sha"][:12] if commits else ref
    return sha, archive_url(repo, ref)


# -- checking without the API at all -----------------------------------------
#
# Everything above spends the REST quota carefully. This spends none of it.
#
# Two questions still reach the API in the ordinary path: which commit last
# touched a folder, and what a release has attached to it. Both have an answer
# that costs no quota, because the archive itself is served from codeload,
# which is not the API:
#
#   the version of a folder      hash what is inside it, in the archive
#   the folders a repo holds     read them out of the archive
#
# What it cannot do is see a release. A release asset is a file the author
# UPLOADED; it is not in the repository, so no amount of downloading the
# repository will find it. An addon checked this way therefore follows its
# default branch instead of its releases, and installs the source tree rather
# than the author's packaged zip. That is a real difference and it is why this
# is a checkbox rather than the default.
#
# The cost is bandwidth in place of quota. It is smaller than it sounds: a
# digest is stored against the commit it was taken from, so it is computed once
# ever for a given commit, and the ref advertisement (free) means an archive is
# only fetched when the branch has actually moved.

_recent_archive: dict[str, bytes] = {}


def archive_bytes(url: str) -> bytes:
    """The archive at `url`, reusing the last one when it is the same.

    A check made without the API downloads an archive to work out a version and
    the install immediately wants the same bytes. Keeping exactly one is enough to make
    that one download instead of two, without holding a pile of repositories in
    memory -- which, for a mode whose whole cost is bandwidth, would be a poor
    trade.
    """
    if url in _recent_archive:
        return _recent_archive[url]
    blob = download(url)
    _recent_archive.clear()
    _recent_archive[url] = blob
    return blob


def without_wrapper(names: list[str]) -> str:
    """The single directory a GitHub archive wraps everything in, or ''.

    `codeload` hands back `repo-main/...`; the wrapper is an artefact of how
    the archive is built and is not part of any path in the repository.
    """
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    return tops.pop() + "/" if len(tops) == 1 else ""


def digests_in_archive(blob: bytes) -> dict[str, str]:
    """A content digest for every addon folder in an archive.

    Every folder at once, deliberately: nine addons in one repository would
    otherwise mean nine downloads of the same archive to hash them one at a
    time. Digesting the lot costs one pass over bytes already in hand.

    A folder is an addon when it holds a .toc named after itself -- the same
    rule the installer and the repository listing apply, so this cannot promise
    something the install would then not find. One level of nesting is allowed
    (`src/MyAddon`); a candidate sitting inside another is a bundled library
    (`MyAddon/Libs/AceGUI-3.0`) and is not offered separately.

    A digest covers EVERYTHING under the folder, libraries included -- a change
    inside `MyAddon/Libs` is a change to MyAddon, and an addon that did not
    notice its own bundled code moving would be worse than no digest at all.
    So it moves when and only when that addon's own files move, which is a
    closer answer than "the last commit that touched it".
    """
    import hashlib

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        listed = archive.namelist()
        lead = without_wrapper(listed)
        paths = {}
        for name in listed:
            if name.endswith("/"):
                continue
            path = name[len(lead):] if name.startswith(lead) else name
            if path:
                paths[path] = name

        candidates = set()
        for path in paths:
            if "/" not in path:
                continue
            folder = path.rsplit("/", 1)[0]
            if folder.count("/") > 1:
                continue
            leaf = folder.rsplit("/", 1)[-1]
            if path.rsplit("/", 1)[-1].lower() == f"{leaf.lower()}.toc":
                candidates.add(folder)

        digests = {}
        for folder in sorted(candidates):
            if any(folder.startswith(other + "/") for other in candidates if other != folder):
                continue
            digest = hashlib.sha256()
            for path in sorted(p for p in paths if p.startswith(folder + "/")):
                digest.update(path.encode())
                digest.update(archive.read(paths[path]))
            digests[folder] = digest.hexdigest()[:12]
    return digests


def digest_key(repo: str, sha: str, folder: str) -> str:
    return f"digest:{repo}@{sha}:{folder}"


def folder_digest(repo: str, ref: str, folder: str) -> str:
    """This folder's version, worked out from the archive rather than the API.

    Kept against the commit it was taken from, so it is never stale and never
    computed twice: the same commit always has the same contents.
    """
    global _store_dirty
    sha = ref_sha(repo, ref) or ref
    known = store().get(digest_key(repo, sha, folder))
    if known and known.get("digest"):
        touch(digest_key(repo, sha, folder))
        return known["digest"]

    blob = archive_bytes(archive_url(repo, ref, tag=names_a_tag(repo, ref)))
    digests = digests_in_archive(blob)
    for name, value in digests.items():
        store()[digest_key(repo, sha, name)] = {"digest": value, "at": time.time()}
    _store_dirty = True
    if folder not in digests:
        die(f"nothing in {repo} holds '{folder}' -- check the folder name")
    return digests[folder]


def ref_without_api(repo: str, branch: str | None) -> str:
    """Which ref to follow, without asking the API which one is default."""
    found = git_refs(repo)
    if not found:
        die(f"cannot reach git for {repo}.\n"
            "     Checking without the GitHub API needs github.com reachable;\n"
            "     untick that box to use the API instead.")
    ref = branch or found.get("head")
    if not ref or ref_sha(repo, ref) is None:
        die(f"no branch or tag '{branch or found.get('head')}' in {repo}")
    return ref


def version_without_api(repo_spec: str) -> tuple[str, str]:
    """(version, archive url) for a source, spending no REST quota at all.

    Follows the default branch rather than releases -- see the note above.
    """
    repo, branch, folder = split_repo_spec(repo_spec)
    ref = ref_without_api(repo, branch)
    url = archive_url(repo, ref, tag=names_a_tag(repo, ref))

    chosen = [pick for pick in wanted_folders(folder) if not names_a_toc(pick)]
    if not chosen:
        # No folder named: the ref's own commit is the version, and the ref
        # advertisement already told us it. Nothing is downloaded to check.
        return ref_sha(repo, ref)[:12], url

    version = folder_digest(repo, ref, chosen[0])
    for extra in chosen[1:]:
        version = f"{version}+{folder_digest(repo, ref, extra)[:7]}"
    return version, url


def root_tocs_in_archive(blob: bytes) -> list[str]:
    """The files sitting at the top level of an archive, GitHub's wrapper stripped.

    Only the names, and only that level: what this is for is spotting a
    repository that IS the addon and ships a .toc per client.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        listed = archive.namelist()
        lead = without_wrapper(listed)
        found = []
        for name in listed:
            if name.endswith("/"):
                continue
            path = name[len(lead):] if name.startswith(lead) else name
            if path and "/" not in path:
                found.append(path)
        return found


def addons_in_repo_without_api(repo_spec: str) -> list[str]:
    """The addon folders a repository holds, read out of its archive."""
    repo, branch, _folder = split_repo_spec(repo_spec)
    ref = ref_without_api(repo, branch)
    blob = archive_bytes(archive_url(repo, ref, tag=names_a_tag(repo, ref)))
    root = root_tocs_in_archive(blob)
    # Same rule as the API path: a .toc at the root means everything below it
    # belongs to that one addon.
    found = [] if any(names_a_toc(name) for name in root) else sorted(
        digests_in_archive(blob), key=str.lower)
    return found + toc_alternatives(root)


def addons_in_repo(repo_spec: str, *, no_api: bool = False) -> list[str]:
    """The addon folders a repository holds, for somebody to choose from.

    A folder counts as an addon when it holds a .toc named after itself -- the
    same rule the installer applies to an archive, so the list cannot promise
    something the install would then not find.

    One request, not one per folder: the git trees API returns the whole file
    list at a ref. The contents API would need a call per directory, which for
    a repository of nine addons is nine round trips to draw one list.

    Bundled libraries are excluded by depth. `MyAddon/Libs/AceGUI-3.0` holds
    AceGUI-3.0.toc and is an addon by the letter of the rule, but nobody
    choosing what to install means it -- and installing it separately is the
    mistake `addon_dirs_in` is bounded to avoid.
    """
    if no_api:
        return addons_in_repo_without_api(repo_spec)

    repo, branch, _folder = split_repo_spec(repo_spec)
    ref = branch or default_branch(repo)
    tree = http_json(f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1")
    if not tree:
        die(f"cannot read {repo} at {ref}")

    tocs, directories = {}, set()
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") == "tree":
            directories.add(path)
        elif item.get("type") == "blob" and path.lower().endswith(".toc"):
            tocs.setdefault(path.rsplit("/", 1)[0] if "/" in path else "", []).append(path)

    root = tocs.get("", [])
    found = []
    # A .toc at the root means the repository IS the addon, and everything
    # under it is that addon's own files -- including its bundled libraries.
    # NotPlater carries libs-2.4.3/LibSharedMedia-3.0/LibSharedMedia-3.0.toc,
    # which is an addon by the letter of the rule and is nobody's answer to
    # "which of these do you want installed".
    if not root:
        for directory, paths in tocs.items():
            if not directory or directory.count("/") > 1:
                continue
            name = directory.rsplit("/", 1)[-1]
            if any(path.rsplit("/", 1)[-1] == f"{name}.toc" for path in paths):
                found.append(directory)

    # A root holding several .toc files is one addon with several names, one
    # per client. Each is a real install target and the choice between them is
    # not one this tool can make for somebody -- it is which game they play.
    found.extend(toc_alternatives(root))

    if tree.get("truncated") and not found:
        # Enormous repository. Say so rather than showing an empty list, which
        # would read as "this repo has no addons in it".
        die(f"{repo} is too large to list; name the folder yourself")
    return sorted(found, key=str.lower)


def toc_flavour_base(stems: list[str]) -> str | None:
    """The single name a set of .toc stems shares, if they are a flavour set.

    The WoW convention, and what ayro-CMD/FrostSeek does: FrostSeek.toc beside
    FrostSeek_Wrath.toc, FrostSeek_Cata.toc and four more, all in ONE folder
    called FrostSeek. The client picks the .toc that matches itself; the folder
    is named after the base. Splitting those into separate installs would break
    every one of them.

    Recognised by shape, because that is what the convention is: one stem that
    every other extends with a separator. Returns None when no stem does --
    which is the other kind of repository entirely, and the reason this is a
    function rather than a rule of thumb.
    """
    if not stems:
        return None
    base = min(stems, key=lambda stem: (len(stem), stem.lower()))
    for other in stems:
        if other == base:
            continue
        if not other.lower().startswith(base.lower()):
            return None
        if other[len(base):len(base) + 1] not in "-_.":
            return None
    return base


def toc_alternatives(names: list[str]) -> list[str]:
    """Root .toc files that are alternative NAMES for one addon, or none at all.

    RichSteini/NotPlater is one addon in one root holding NotPlater-2.4.3.toc
    and NotPlater-3.3.5.toc -- one per client, with no base .toc between them.
    Neither is a suffix any client understands: 3.3.5 has no notion of flavour
    .tocs at all, it loads <Folder>/<Folder>.toc and nothing else. So the
    folder has to be NAMED after the one you want, which makes this a question
    only the person playing can answer -- and getting it wrong is quiet, because
    an addon built for the wrong client is not something the game reports.

    Empty when there is nothing to choose: one .toc, or a flavour set that the
    client itself picks between (see `toc_flavour_base`).
    """
    tocs = sorted((name for name in names if names_a_toc(name)), key=str.lower)
    if len(tocs) < 2 or toc_flavour_base([addon_name_for(name) for name in tocs]):
        return []
    return tocs


def likely_addon(name: str, folders: list[str]) -> str | None:
    """Which of a repository's folders is probably this addon.

    An exact name match, else a case-insensitive one, else nothing. Nothing is
    a perfectly good answer: a wrong guess that arrives pre-ticked is worse
    than no guess, because it will be accepted without being read.
    """
    if not folders:
        return None
    for folder in folders:
        if addon_name_for(folder) == name:
            return folder
    lowered = name.lower()
    for folder in folders:
        if addon_name_for(folder).lower() == lowered:
            return folder
    return None


def install_plan(repo_spec: str, chosen: list[str], available: list[str]) -> list[tuple[str, str]]:
    """The rows to create for installing a repository: (addon name, source).

    One row per addon, never one row for a repository holding several. Binding
    the whole repo works and is almost never meant: every addon in it would be
    written into AddOns whenever this one row updates, and each would report an
    update whenever any of them changed. Choosing is what the tick boxes are
    for, and this is where a choice becomes rows.

    A repository holding ONE addon is bound without naming its folder, even
    though the folder is known. Naming it switches the row off the
    repository's releases and onto the last commit touching that folder, so an
    addon that publishes tagged releases would start reporting commit ids
    instead of version numbers -- for an install of exactly the same files.

    The row is named after the folder that will land in AddOns, not after the
    repository: that name is what the client loads, what a rescan will find,
    and therefore the only name under which the two can agree.
    """
    repo, branch, folder = split_repo_spec(repo_spec)
    base = f"github:{repo}" + (f"@{branch}" if branch else "")
    # A folder named in the pasted URL is a choice too -- clicking into one
    # addon of several on github.com and copying the address is the clearest
    # way anybody states which one they mean.
    picks = wanted_folders(",".join(chosen)) or wanted_folders(folder)
    if len(available) <= 1 and len(picks) <= 1 and not any(names_a_toc(p) for p in picks):
        named = picks or available or [repo.rsplit("/", 1)[-1]]
        return [(addon_name_for(named[0]), base)]
    # A .toc pick keeps its name in the source even when it is the only one
    # ticked: it is not the folder that would be installed by default, it is
    # one of several names the same files can be installed under.
    return [(addon_name_for(pick), f"{base}#{pick}") for pick in picks]


def rename_entry(install: dict, old: str, new: str) -> bool:
    """Move a row to the name of the folder that was actually installed.

    An install names its row before it can know what the archive holds: the
    best guess is the repository's name, and a repository called
    NotPlater-3.3.5 whose addon is NotPlater makes that guess wrong. Left
    alone, the manifest then holds a bound row naming a folder that does not
    exist -- reported as *not installed* -- and the next rescan adds a second,
    unmanaged row for the folder that does. Two rows, one addon, neither of
    them right.

    Refuses to overwrite a row holding a decision of somebody's: an unmanaged
    row is only a note that a folder exists, and this replaces it with the
    truth, but a bound row is a binding that was set on purpose.
    """
    entries = install.setdefault("addons", {})
    if old == new or old not in entries:
        return False
    if entries.get(new, {}).get("source", "unmanaged") != "unmanaged":
        return False
    entries[new] = entries.pop(old)
    return True


def settle_names(install: dict, names: list[str]) -> list[tuple[str, str]]:
    """Rename fresh rows to the folder that actually landed in AddOns.

    An install has to name its row before it can know what the archive holds,
    and a repository called NotPlater-3.3.5 whose addon is NotPlater makes that
    guess wrong. Left alone the manifest holds a bound row naming a folder that
    does not exist, and the next scan adds a second row for the folder that
    does.
    """
    entries = install.get("addons", {})
    moved = []
    for name in names:
        folders = entries.get(name, {}).get("folders") or []
        if len(folders) == 1 and folders[0] != name and rename_entry(install, name, folders[0]):
            moved.append((name, folders[0]))
    return moved


def download(url: str) -> bytes:
    """Fetch one archive: off the API's meter where possible, paced where not.

    An archive from codeload costs no quota, so it is tried first and fetched
    at full speed. A release asset served from github.com is the same. Only a
    REST zipball is spent out of the hourly budget, and only that is paced.
    """
    # A release asset asked for by id: say we want the bytes, not the JSON
    # describing them.
    accept = "application/octet-stream" if "/releases/assets/" in url else None
    try:
        return fetch(url, accept)
    except (urllib.error.HTTPError, urllib.error.URLError):
        # codeload declining is not the end of the road: the REST API can serve
        # the same ref, for the price of a call. Silent on purpose -- which
        # host answered is a transport detail, and the addon still installs.
        fallback = rest_archive_url(url)
        if fallback is None:
            raise
        return fetch(fallback)


# Where an Authorization header is ours to send. Everywhere else it is either
# useless or actively harmful -- see `TokenSafeRedirect`.
GITHUB_HOSTS = ("api.github.com", "codeload.github.com", "github.com")


def authenticable(url: str) -> bool:
    """Whether this host is GitHub, and so may be sent our token.

    The parsed hostname, never a substring of the URL: `github.com.evil.example`
    contains "github.com", and so does `https://api.github.com@evil.example/`,
    where the part before the @ is a username and the host is the other one.
    """
    return urllib.parse.urlsplit(url).hostname in GITHUB_HOSTS


class TokenSafeRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect leaves GitHub.

    Downloading a private release asset means asking the API for it and being
    sent on to a signed URL on objects.githubusercontent.com. urllib copies
    every header onto the redirected request, and that storage host rejects a
    request carrying both its own signature and an Authorization header --
    "only one auth mechanism allowed", a 400 on what is otherwise a correct
    download. Stripping it is also the safer default in its own right: a token
    should not be handed to a host that did not need it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is not None and not authenticable(newurl):
            # Header names are capitalised by urllib on the way in, so remove
            # the form it actually stored rather than the one we added.
            for name in ("Authorization", "authorization"):
                following.headers.pop(name, None)
        return following


# Installed globally rather than kept as an opener of our own, so that every
# request in this module -- the API, the git ref advertisement, downloads --
# gets the same treatment from the same plain `urlopen` call, and so that the
# one thing the tests replace is still the one thing every path goes through.
urllib.request.install_opener(urllib.request.build_opener(TokenSafeRedirect))


def fetch(url: str, accept: str | None = None) -> bytes:
    through_the_api = urllib.parse.urlsplit(url).hostname == "api.github.com"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    token = github_token()
    if token and authenticable(url):
        # codeload and github.com are included deliberately: a private
        # repository serves neither anonymously, and sending the token means
        # the free host can answer instead of falling through to the REST API
        # and spending a call. If a host declines the header, `download`
        # already falls back, so the worst case is what happened before.
        request.add_header("Authorization", f"Bearer {token}")
    if accept is not None:
        # A release asset URL on the API returns JSON *about* the asset unless
        # this says otherwise. Without it the "zip" is a metadata document.
        request.add_header("Accept", accept)
    if through_the_api:
        if THROTTLE.spent():
            die(rate_limit_message(THROTTLE.reset_at))
        THROTTLE.wait_turn()
    with urllib.request.urlopen(request, timeout=120) as response:
        if through_the_api:
            THROTTLE.observe(response.headers)
        return response.read()


# ── saved variables (the WTF folder) ─────────────────────────────────────────
# An addon's settings do not live with the addon. The client keeps them in
#
#   WTF/Account/<ACCOUNT>/SavedVariables/<Addon>.lua              account-wide
#   WTF/Account/<ACCOUNT>/<Realm>/<Char>/SavedVariables/<Addon>.lua  per character
#
# plus a .lua.bak the client rotates beside each. Replacing an addon never
# touches any of that, which is almost always what you want -- your bars stay
# where you put them across an update. The exception is the reason this exists:
# settings written by a different fork of the addon, or a version old enough
# that the new one chokes on them, and the fix is to start clean.
#
# Nothing here is called unless somebody ticks a box that says so. Deleting
# settings is not a side effect of installing anything.


def _named(path: Path, name: str) -> list[Path]:
    """Directories inside `path` called `name`, whatever case they are in.

    The client is case-blind about all of this -- Windows because its
    filesystem is, a Wine install because Wine makes it so -- and real WTF
    folders in the wild are spelled Account, ACCOUNT and account.
    """
    try:
        return [child for child in path.iterdir()
                if child.is_dir() and child.name.lower() == name.lower()]
    except OSError:
        return []


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted((child for child in path.iterdir() if child.is_dir()),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def wtf_dir(root: Path) -> Path | None:
    """The WTF folder belonging to the client whose AddOns folder this is.

    AddOns is <WoW>/Interface/AddOns, so WTF is two levels up. Found rather
    than assumed: a folder that is not there is a perfectly ordinary answer for
    a client that has never been run.
    """
    for candidate in _named(root.parent.parent, "WTF"):
        return candidate
    return None


def saved_variable_dirs(wtf: Path) -> list[Path]:
    """Every SavedVariables folder in a WTF tree: one per account, one per character."""
    found = []
    for accounts in _named(wtf, "Account"):
        for account in _subdirs(accounts):
            found.extend(_named(account, "SavedVariables"))
            for realm in _subdirs(account):
                if realm.name.lower() == "savedvariables":
                    continue
                for character in _subdirs(realm):
                    found.extend(_named(character, "SavedVariables"))
    return found


def saved_variables(root: Path, addon: str) -> list[Path]:
    """Every settings file the client keeps for one addon, account and character.

    The .lua.bak beside each is included: it is the client's own previous copy,
    and leaving it behind while deleting the .lua would have the addon come
    back with the settings you just asked to be rid of.
    """
    wtf = wtf_dir(root)
    if wtf is None:
        return []
    wanted = (f"{addon.lower()}.lua", f"{addon.lower()}.lua.bak")
    found = []
    for directory in saved_variable_dirs(wtf):
        try:
            found.extend(child for child in directory.iterdir()
                         if child.is_file() and child.name.lower() in wanted)
        except OSError:
            continue
    return sorted(found, key=lambda p: str(p).lower())


def remove_saved_variables(paths: list[Path], *, backup: bool) -> tuple[list[Path], list[str]]:
    """Delete these files, keeping a copy of each first if asked.

    Returns (deleted, problems). A copy is `<file>.replaced`, beside the
    original: the client loads <Addon>.lua and nothing else, so a file with
    another name sitting next to it is invisible to the game and obvious to
    whoever goes looking for it.

    A file that cannot be removed is reported, not raised: one locked file must
    not leave the rest half-done and the caller with no idea which half.
    """
    deleted, problems = [], []
    for path in paths:
        try:
            if backup:
                shutil.copy2(path, backup_name(path))
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            problems.append(f"{path.name}: {exc.strerror or exc}")
    return deleted, problems


# ── installing ───────────────────────────────────────────────────────────────


def addon_dirs_in(tree: Path, depth: int = 1) -> list[tuple[Path, str]]:
    """Find the real addon folders inside an extracted archive.

    Returns (folder, name-to-install-as) pairs. Three layouts turn up and all
    three have to work:

      MyAddon/MyAddon.toc                 a packaged release
      repo-1a2b3c/MyAddon/MyAddon.toc     GitHub's source archive
      repo-1a2b3c/MyAddon.toc             a repo whose root *is* the addon

    The third is why the name is a separate return value: installing that one
    under its folder name would put `someone-MyAddon-1a2b3c` in AddOns, which
    the client silently ignores because it does not match the .toc inside.

    The order below matters. A .toc at the current level has to be checked
    before descending into a lone subdirectory, or an addon laid out as
    MyAddon.toc + Core/ gets followed down into Core/ and lost.

    `depth` bounds the last resort -- searching subdirectories when this level
    holds nothing recognisable. One level covers the real layouts (src/, Addons/,
    an addon beside docs/) and stops short of an addon's own bundled libraries,
    which sit deeper and would otherwise be installed as if each were the addon.
    """
    hits = [
        (child, child.name)
        for child in sorted(tree.iterdir())
        if child.is_dir() and find_toc(child) is not None
    ]
    if hits:
        return hits

    tocs = sorted(tree.glob("*.toc"))
    if tocs:
        # A .toc named after this folder settles it: that is the one the client
        # would load out of a folder by this name.
        exact = [toc for toc in tocs if toc.stem.lower() == tree.name.lower()]
        if exact:
            return [(tree, exact[0].stem)]
        # Several .tocs with no base between them are alternative NAMES for
        # this one tree, one per client. Returned as separate candidates rather
        # than guessed between: the rule used to be "shortest stem", which
        # quietly installed NotPlater's 2.4.3 build for somebody on 3.3.5.
        alternatives = toc_alternatives([toc.name for toc in tocs])
        if alternatives:
            return [(tree, addon_name_for(name)) for name in alternatives]
        # A flavour set, or a single .toc: one folder, named after the base,
        # holding all of them -- the client reads the one that matches itself.
        return [(tree, toc_flavour_base([toc.stem for toc in tocs]))]

    subdirs = sorted(child for child in tree.iterdir() if child.is_dir())
    if len(subdirs) == 1:
        return addon_dirs_in(subdirs[0], depth=depth)

    # Nothing at this level and several ways down. Repositories do lay addons
    # out as src/MyAddon/MyAddon.toc beside a docs/ or .github/ folder, and
    # giving up here reported "no addon folder found" for a repo plainly
    # holding one. Look one level deeper, but only that far: an addon bundles
    # its own libraries (Libs/AceGUI-3.0/AceGUI-3.0.toc), and a search that
    # kept descending would install those as though they were the addon.
    if depth > 0:
        candidates = {}
        for child in subdirs:
            if child.name.startswith("."):
                continue
            found = addon_dirs_in(child, depth=depth - 1)
            if found:
                candidates[child.name] = found
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            # Several ways down, each holding an addon. This is what a repo with
            # a folder per client looks like -- Wrath/MyAddon, Retail/MyAddon --
            # and picking one would be a coin toss decided by sort order. It
            # really did install Retail on a Wrath realm, silently, and an addon
            # built for the wrong client is not an error the game reports.
            names = ", ".join(sorted(candidates))
            die(f"this repo holds an addon in more than one folder ({names}). "
                f"Name the one you want: #{sorted(candidates)[0]}")
    return []


def descend_to(tree: Path, folder: str) -> Path:
    """Find one named folder inside an extracted archive.

    GitHub's source archive wraps everything in `owner-repo-1a2b3c/`, so the
    path the user picked on github.com is one level down from the archive root.
    Both are tried rather than assuming, because a hand-rolled release zip may
    have no wrapper at all.
    """
    candidates = [tree / folder]
    subdirs = [child for child in tree.iterdir() if child.is_dir()]
    if len(subdirs) == 1:
        candidates.append(subdirs[0] / folder)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    die(f"the archive has no folder '{folder}' -- was it renamed or moved?")


def descend_to_toc(tree: Path, toc: str) -> Path:
    """The level of an extracted archive holding a named .toc file.

    The same wrapper problem as `descend_to`: GitHub's source archive puts
    everything one level down inside `owner-repo-1a2b3c/`, and a hand-rolled
    release zip may have no wrapper at all.
    """
    subdirs = [child for child in tree.iterdir() if child.is_dir()]
    candidates = [tree] + (subdirs if len(subdirs) == 1 else [])
    for candidate in candidates:
        if any(child.name.lower() == toc.lower() and child.is_file()
               for child in candidate.iterdir()):
            return candidate
    die(f"the archive has no '{toc}' at its root -- was it renamed or moved?")


def install_zip(
    blob: bytes,
    target: Path,
    dry_run: bool,
    *,
    backup: bool = False,
    entry: dict | None = None,
    only: str | list[str] | None = None,
    report=None,
) -> list[str]:
    """Unpack an addon archive into AddOns. Returns the folder names written.

    Under `dry_run` nothing is written but the names are still returned, so a
    caller can report exactly what it would have installed without this needing
    to know how that report is displayed.

    `only` narrows the archive to named folders inside it, for a repository
    that holds several addons -- one name, a comma-separated string, or a list.
    Without it every addon folder in the archive is installed, which is right
    for an addon shipping its own library and wrong for a repository of nine
    unrelated addons.

    `backup` moves an existing real folder aside instead of deleting it.
    `entry` refines that PER FOLDER: the decision used to be taken once from
    the bound addon and applied to every folder the archive landed, so updating
    one addon of nine deleted the other eight on the strength of a record that
    described only the first. Confirmed by losing a file. See
    `should_backup_folder`.

    This used to delete unconditionally while the window promised a backup, and
    somebody lost a hand-installed addon to it. Do not make that true again.
    """
    report = report or _nothing
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                # Refuse path traversal rather than trusting the archive; this
                # unpacks whatever a third-party repo published.
                for name in archive.namelist():
                    resolved = (tmpdir / name).resolve()
                    if not str(resolved).startswith(str(tmpdir.resolve())):
                        die(f"archive contains an unsafe path: {name}")
                archive.extractall(tmpdir)
        except zipfile.BadZipFile:
            die("downloaded file is not a zip -- check the source is an addon release")

        chosen = wanted_folders(only) if isinstance(only, str) else list(only or [])
        if chosen:
            folders = []
            for name in chosen:
                if names_a_toc(name):
                    # Not a folder in the archive: a name for the whole of it.
                    folders.append((descend_to_toc(tmpdir, name), addon_name_for(name)))
                    continue
                found = addon_dirs_in(descend_to(tmpdir, name))
                if not found:
                    die(f"no addon folder (a directory holding its own .toc) found in '{name}'")
                folders.extend(found)
        else:
            folders = addon_dirs_in(tmpdir)
            if not folders:
                die("no addon folder (a directory holding its own .toc) found in the archive")
            alternatives = {name for tree, name in folders}
            if len({tree for tree, _name in folders}) == 1 and len(alternatives) > 1:
                # One set of files, several names it could be installed under:
                # a repository that is the addon and ships a .toc per client.
                # Installing all of them would put the same addon in AddOns
                # two or three times over, under names only one of which the
                # person meant -- so this is a question, not a default.
                names = ", ".join(sorted(alternatives))
                die(f"this repository holds one addon with {len(alternatives)} .toc files "
                    f"({names}) -- one per client version.\n"
                    f"     Name the one you want: #{sorted(alternatives)[0]}.toc")

        written = []
        for folder, name in folders:
            if "/" in name or name in ("", ".", ".."):
                die(f"archive names an unusable addon folder: {name!r}")
            destination = target / name
            if not dry_run:
                # is_link first, always: rmtree on a junction would delete
                # through it into whatever it points at.
                if is_link(destination):
                    remove_link(destination)
                elif destination.exists():
                    keep = backup and (entry is None or should_backup_folder(entry, name))
                    displace(destination, backup=keep, report=report)
                shutil.copytree(folder, destination)
            written.append(name)
        return written


def displace(destination: Path, *, backup: bool, report=None) -> None:
    """Clear a real folder out of the way, keeping it if asked.

    One implementation for both install paths. They used to differ -- a local
    source moved the folder aside, an archive deleted it outright -- which meant
    the answer to "will this destroy my addon?" depended on which kind of source
    you happened to pick, and the window gave the same answer for both.
    """
    report = report or _nothing
    if not backup:
        report("note", f"replacing {destination.name}")
        shutil.rmtree(destination)
        return
    moved = backup_name(destination)
    report("note", f"moving existing folder aside -> {moved.name}")
    destination.rename(moved)


def covers_several_addons(entry: dict) -> bool:
    """Is this row bound to a whole repository that holds several addons?

    Answered from what the last install actually wrote, not from the source
    text: a `github:` source naming no folder MIGHT hold one addon or nine, and
    the difference is only knowable once the archive has been unpacked. The
    folder list is that answer, already recorded.
    """
    if not entry.get("source", "unmanaged").startswith("github:"):
        return False
    if wanted_folders(split_repo_spec(entry["source"][len("github:"):])[2]):
        return False
    return len(entry.get("folders") or []) > 1


def should_backup_folder(entry: dict, folder: str) -> bool:
    """Whether THIS folder should be kept when the next install replaces it.

    `should_backup` asks the same question about the addon as a whole, which is
    the same answer right up until an archive lands more than one folder. Then
    it is wrong for every folder but the first: updating one addon of nine
    deleted the other eight, because the entry said "this tool installed it"
    and that was only ever true of the folder the entry named.

    A folder counts as this tool's own when the entry records a version AND
    lists that folder among the ones it wrote. Both halves matter: a scanned
    entry lists the folder before anything has been installed into it, so the
    version is what separates "we put this here" from "it was already there".
    """
    if not entry.get("backup", True):
        return False
    if not entry.get("installed"):
        return True
    return folder not in (entry.get("folders") or [])


def should_backup(entry: dict) -> bool:
    """Whether this addon's next install should keep what is already there.

    Two rules, and the second is the one that stops `.replaced2`, `.replaced3`
    piling up in an AddOns folder:

      a user who turned backups off never gets one
      otherwise, back up only what this tool did not install itself

    Anything with a recorded `installed` version is a folder this tool wrote,
    so replacing it loses nothing that was not already fetched from the source.
    A folder with no recorded version is the user's own -- installed by hand,
    or there before they ever ran this -- and that is worth keeping, once.
    """
    if not entry.get("backup", True):
        return False
    return not entry.get("installed")


def install_local(source_path: Path, target: Path, mode: str, dry_run: bool, *, backup: bool = True, report=None) -> list[str]:
    """Link (or copy) a folder from this disk into AddOns."""
    report = report or _nothing
    if not source_path.is_dir():
        die(f"no such folder: {source_path}")
    if find_toc(source_path) is None:
        report("warn", f"{source_path} has no {source_path.name}.toc -- installing anyway")

    destination = target / source_path.name
    if dry_run:
        return [source_path.name]

    if is_link(destination):
        remove_link(destination)
    elif destination.exists():
        displace(destination, backup=backup, report=report)

    if mode == "copy":
        shutil.copytree(source_path, destination)
    else:
        make_link(source_path.resolve(), destination)
    return [source_path.name]


def backup_name(destination: Path) -> Path:
    """Where `install_local` would move an existing real folder aside to.

    Exposed rather than inlined because a GUI has to name it *before* doing it:
    in a terminal you read the log afterwards, in a window you have to be told
    in the confirm step or you never find out at all.
    """
    backup = destination.with_name(destination.name + ".replaced")
    counter = 1
    while backup.exists():
        counter += 1
        backup = destination.with_name(f"{destination.name}.replaced{counter}")
    return backup


def displaced_folder(entry: dict, addon: str, root: Path) -> Path | None:
    """The real folder this entry's next install would replace, if any.

    A link is replaced silently -- it holds no files of its own. A real
    directory is either somebody's manual install or one this tool wrote, and
    telling them apart is `should_backup`'s job, not this one's.

    For a `local:` source the folder is named after the SOURCE, because that is
    what lands in AddOns. For `github:` the archive's contents are unknowable
    until it is downloaded, so the addon's own name is the best guess.
    """
    destination = install_destination(entry, addon, root)
    if destination is not None and destination.exists() and not is_link(destination):
        return destination
    return None


def install_destination(entry: dict, addon: str, root: Path) -> Path | None:
    """The folder in AddOns this entry's next install would write.

    For a `local:` source the folder is named after the SOURCE, because that is
    what lands in AddOns. For `github:` a named folder is a much better guess
    than the addon's own name -- it is what lands in AddOns, and for a repo of
    several addons the two are often different. Without one the archive's
    contents are unknowable until it is downloaded, so the addon's name remains
    the best guess.
    """
    source = entry.get("source", "unmanaged")
    if source.startswith("local:"):
        return root / Path(source[len("local:"):]).name
    if source.startswith("github:"):
        _repo, _branch, folder = split_repo_spec(source[len("github:"):])
        return root / (addon_name_for(folder) if folder else addon)
    return None


def will_displace(entry: dict, root: Path, addon: str = "") -> Path | None:
    """Where the displaced folder would be moved to, or None if nothing is kept.

    None now means two different things -- nothing is at risk, or it is about to
    be deleted rather than kept -- so callers that need to warn should ask
    `displaced_folder` as well.
    """
    destination = displaced_folder(entry, addon, root)
    if destination is None or not should_backup(entry):
        return None
    return backup_name(destination)


# ── updating one addon ───────────────────────────────────────────────────────

UNMANAGED = "unmanaged"
UP_TO_DATE = "up-to-date"
CHANGED = "changed"
FAILED = "failed"


@dataclass
class Result:
    """What happened to one addon. Carries text; never prints it."""

    name: str
    outcome: str
    detail: str = ""
    version: str | None = None
    previous: str | None = None
    folders: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.outcome == FAILED


def update_addon(
    name: str,
    entry: dict,
    root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    check: bool = False,
    no_api: bool = False,
    progress=None,
) -> Result:
    """Update a single addon. Returns what happened; never prints.

    `entry` is mutated in place on success (installed version, folder list) but
    the manifest is NOT saved -- the caller owns that, because saving once per
    addon would multiply writes and saving on failure would be wrong.

    progress: optional callable taking (stage, detail) so a front end can show
    "checking...", "downloading...", "installing..." without core knowing what
    a terminal or a progress bar is.

    Every exception becomes a FAILED result rather than propagating. That is
    deliberate and it is the rule the whole tool rests on: one unreachable,
    private or renamed repo must not abort the run, because aborting would also
    drop every manifest change made before it and undo the successes ahead of it.
    """
    progress = progress or _nothing
    result = Result(name=name, outcome=CHANGED, previous=entry.get("installed"))

    def report(level: str, message: str) -> None:
        result.notes.append((level, message))

    source = entry.get("source", "unmanaged")
    if source == "unmanaged":
        return Result(name=name, outcome=UNMANAGED, detail="no source set")

    try:
        kind, rest = parse_source(source)
        if kind == "local":
            # Nothing to fetch: a link is already live, and a copy is refreshed
            # straight from disk.
            mode = entry.get("mode", "link")
            progress("installing", rest)
            result.folders = install_local(
                Path(rest), root, mode, dry_run, backup=should_backup(entry), report=report
            )
            result.version = "linked" if mode == "link" else "copied"
            result.detail = (f"would {mode} from {rest}" if dry_run else f"{mode}ed from {rest}")
            if not dry_run:
                entry["folders"] = result.folders
                entry["installed"] = result.version
                entry.pop("missing", None)
            return result

        progress("checking", rest)
        _repo, _branch, folder = split_repo_spec(rest)
        version, url = version_without_api(rest) if no_api else latest_github(rest)
        result.version = version
        if entry.get("installed") == version and not force:
            return Result(name=name, outcome=UP_TO_DATE, detail=f"up to date ({version})", version=version)

        result.detail = f"{result.previous or 'not installed'} -> {version}"
        if check:
            return result

        progress("downloading", f"{rest} {version}")
        # Offline checking has usually just fetched this exact archive to work
        # the version out; reuse those bytes rather than paying for them twice.
        blob = archive_bytes(url) if no_api else download(url)
        progress("installing", rest)
        # backup= is the user's own preference; the entry decides folder by
        # folder which of them that preference actually applies to.
        result.folders = install_zip(
            blob, root, dry_run,
            backup=entry.get("backup", True), entry=entry, only=folder, report=report,
        )
        if not folder and len(result.folders) > 1:
            # A repository of several addons, bound as a whole. It works, but
            # every addon in it will now report an update whenever any one of
            # them changes, and this addon's entry claims all of their folders.
            # Say so once rather than letting it be discovered as odd behaviour.
            report(
                "note",
                f"this repo holds {len(result.folders)} addons "
                f"({', '.join(result.folders)}). Set the source to one folder "
                f"inside it -- github:{_repo}#FolderName -- to update just this addon.",
            )
        if not dry_run:
            entry["folders"] = result.folders
            entry["installed"] = version
            entry.pop("missing", None)
        return result
    except Exception as exc:
        return Result(
            name=name,
            outcome=FAILED,
            detail=str(exc) or "failed",
            previous=result.previous,
            notes=result.notes,
        )


# ── locating a WoW install ───────────────────────────────────────────────────


def find_addons_dir(given: Path) -> Path:
    """Accept the WoW folder, the Interface folder, or AddOns itself."""
    given = given.expanduser()
    for candidate in (given / "Interface" / "AddOns", given / "AddOns", given):
        if candidate.is_dir() and candidate.name.lower() == "addons":
            return candidate.resolve()
    die(
        f"could not find Interface/AddOns under {given}\n"
        "     Point init at your WoW folder (the one holding Ascension.exe),\n"
        "     or directly at the Interface/AddOns folder itself."
    )
