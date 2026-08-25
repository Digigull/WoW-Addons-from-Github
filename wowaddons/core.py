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

import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
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


def load(windows: bool | None = None) -> dict:
    path = manifest_to_read(windows)
    if not path.exists():
        return {"addons_dir": None, "addons": {}}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"manifest is not valid JSON ({exc}). Fix or delete {path}")


def save(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write-and-move: a manifest half-written by a Ctrl-C is worse than a stale
    # one, since it is the only record of what you bound where.
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(MANIFEST)


def addons_dir(state: dict) -> Path:
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
        toc = child / f"{child.name}.toc"
        if not toc.is_file():
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


def rescan(state: dict, root: Path) -> tuple[int, int]:
    """Fold what is on disk into the manifest. Returns (installed, guessed).

    Does not save -- the caller decides when to write, which matters for a GUI
    that may want to scan speculatively.
    """
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

    # Anything in the manifest that is no longer on disk.
    for name in list(entries):
        if name not in installed:
            entries[name]["missing"] = True

    return len(installed), guessed


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
    if local_path.is_dir() and not (local_path / f"{local_path.name}.toc").is_file():
        candidate = local_path / addon
        if (candidate / f"{addon}.toc").is_file():
            local_path = candidate
        else:
            die(f"{local_path} holds no {addon}/{addon}.toc")
    return f"local:{local_path}", kind, local_path


def set_source(
    state: dict, addon: str, source: str, *, copy: bool = False, backup: bool | None = None
) -> tuple[dict, Path | None]:
    """Bind one addon to a source. Returns (entry, local_path)."""
    entries = state.setdefault("addons", {})
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
    for name, entry in sorted(state.get("addons", {}).items()):
        if entry.get("source") == "unmanaged" and entry.get("suggested"):
            entry["source"] = entry.pop("suggested")
            taken.append((name, entry["source"]))
    return taken


def github_message(exc: urllib.error.HTTPError) -> str:
    """GitHub explains itself in the response body; surface that, not the code."""
    try:
        return json.loads(exc.read().decode()).get("message", "no detail given")
    except Exception:
        return "no detail given"


def http_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        # Entirely optional. Unauthenticated is 60 requests/hour, which is far
        # more than a personal addon list needs; set this only if you hit it.
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        # A 403 is NOT automatically a rate limit -- a private repo, a blocked
        # egress proxy and an exhausted quota all land here, and telling someone
        # to "wait an hour" when the real cause is a proxy wastes their evening.
        # The remaining-quota header is what actually distinguishes them.
        if exc.code in (403, 429):
            if exc.headers.get("x-ratelimit-remaining") == "0":
                reset = exc.headers.get("x-ratelimit-reset", "")
                when = ""
                if reset.isdigit():
                    import datetime

                    when = " until " + datetime.datetime.fromtimestamp(int(reset)).strftime("%H:%M")
                die(
                    f"GitHub rate limit reached{when}."
                    "\n     Set GITHUB_TOKEN to a read-only token to raise it, or wait."
                )
            die(f"GitHub refused the request (403): {github_message(exc)}")
        if exc.code == 401:
            die("GitHub rejected GITHUB_TOKEN (401). Unset it, or use a valid read-only token.")
        die(f"GitHub returned {exc.code} for {url}: {github_message(exc)}")
    except urllib.error.URLError as exc:
        die(f"could not reach GitHub: {exc.reason}")


def split_repo_spec(repo_spec: str) -> tuple[str, str | None, str | None]:
    """'owner/repo@branch#Folder' -> ('owner/repo', 'branch', 'Folder').

    The folder is split off first: a branch name may not contain '#', but a
    path certainly may contain '@' (and nothing stops a branch containing '/'),
    so taking them in the other order would mis-split both.
    """
    folder = None
    if "#" in repo_spec:
        repo_spec, folder = repo_spec.split("#", 1)
        folder = folder.strip("/") or None
    branch = None
    if "@" in repo_spec:
        repo_spec, branch = repo_spec.split("@", 1)
    return repo_spec, branch, folder


def default_branch(repo: str) -> str:
    info = http_json(f"https://api.github.com/repos/{repo}")
    if not info:
        die(f"no such repo, or it is private: {repo}")
    return info.get("default_branch", "master")


def latest_folder_commit(repo: str, branch: str, folder: str) -> str:
    """The last commit that touched one folder -- this addon's real version.

    A repository holding nine addons has one commit history, so its HEAD moves
    whenever ANY of them changes. Versioning an addon by the repo would report
    an update for all nine every time one is touched, and "update available" that
    is usually wrong is worse than no column at all: people stop reading it.

    GitHub answers the narrower question directly, and for the same one request.
    """
    query = urllib.parse.urlencode({"sha": branch, "path": folder, "per_page": 1})
    commits = http_json(f"https://api.github.com/repos/{repo}/commits?{query}")
    if not commits:
        die(f"nothing in {repo} touches '{folder}' -- check the folder name")
    return commits[0]["sha"][:12]


def latest_github(repo_spec: str) -> tuple[str, str]:
    """(version, zip url) for the newest thing at owner/repo[@branch][#folder]."""
    repo, branch, folder = split_repo_spec(repo_spec)

    if folder:
        # A named folder overrides releases deliberately. A release asset is
        # packaged for one addon; there is no reason its contents line up with
        # a path in the source tree, so honouring both would mean guessing.
        ref = branch or default_branch(repo)
        version = latest_folder_commit(repo, ref, folder)
        return version, f"https://api.github.com/repos/{repo}/zipball/{ref}"

    if branch:
        commits = http_json(f"https://api.github.com/repos/{repo}/commits/{branch}")
        if not commits:
            die(f"no branch '{branch}' in {repo}")
        return commits["sha"][:12], f"https://api.github.com/repos/{repo}/zipball/{branch}"

    release = http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if release:
        # Prefer an attached .zip: that is the packaged addon, laid out the way
        # it should sit in AddOns. The source archive is the fallback and needs
        # its wrapper directory stripped, which install_zip handles.
        for asset in release.get("assets", []):
            if asset["name"].lower().endswith(".zip"):
                return release["tag_name"], asset["browser_download_url"]
        return release["tag_name"], release["zipball_url"]

    ref = default_branch(repo)
    commits = http_json(f"https://api.github.com/repos/{repo}/commits/{ref}")
    sha = commits["sha"][:12] if commits else ref
    return sha, f"https://api.github.com/repos/{repo}/zipball/{ref}"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


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
        if child.is_dir() and (child / f"{child.name}.toc").is_file()
    ]
    if hits:
        return hits

    tocs = sorted(tree.glob("*.toc"))
    if tocs:
        # Prefer a .toc named after this folder; otherwise the shortest stem,
        # which skips flavour suffixes like MyAddon-Classic.toc.
        exact = [t for t in tocs if t.stem == tree.name]
        chosen = exact[0] if exact else min(tocs, key=lambda t: len(t.stem))
        return [(tree, chosen.stem)]

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
        for child in subdirs:
            if child.name.startswith("."):
                continue
            found = addon_dirs_in(child, depth=depth - 1)
            if found:
                return found
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


def install_zip(
    blob: bytes,
    target: Path,
    dry_run: bool,
    *,
    backup: bool = False,
    entry: dict | None = None,
    only: str | None = None,
    report=None,
) -> list[str]:
    """Unpack an addon archive into AddOns. Returns the folder names written.

    Under `dry_run` nothing is written but the names are still returned, so a
    caller can report exactly what it would have installed without this needing
    to know how that report is displayed.

    `only` narrows the archive to one folder inside it, for a repository that
    holds several addons. Without it every addon folder in the archive is
    installed, which is right for an addon shipping its own library and wrong
    for a repository of nine unrelated addons.

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

        root = descend_to(tmpdir, only) if only else tmpdir
        folders = addon_dirs_in(root)
        if not folders:
            where = f"'{only}'" if only else "the archive"
            die(f"no addon folder (a directory holding its own .toc) found in {where}")

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
    if not (source_path / f"{source_path.name}.toc").is_file():
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
    source = entry.get("source", "unmanaged")
    if source.startswith("local:"):
        destination = root / Path(source[len("local:"):]).name
    elif source.startswith("github:"):
        # A named folder is a much better guess than the addon's own name: it
        # is what lands in AddOns, and for a repo of several addons the two are
        # often different. Without one the archive's contents are unknowable
        # until it is downloaded, so the addon's name remains the best guess.
        _repo, _branch, folder = split_repo_spec(source[len("github:"):])
        destination = root / (Path(folder).name if folder else addon)
    else:
        return None
    if destination.exists() and not is_link(destination):
        return destination
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
        version, url = latest_github(rest)
        result.version = version
        if entry.get("installed") == version and not force:
            return Result(name=name, outcome=UP_TO_DATE, detail=f"up to date ({version})", version=version)

        result.detail = f"{result.previous or 'not installed'} -> {version}"
        if check:
            return result

        progress("downloading", f"{rest} {version}")
        blob = download(url)
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
