#!/usr/bin/env python3
"""Point a WoW client at whatever repos you want your addons to come from.

    python3 addons.py init ~/Games/Ascension
    python3 addons.py scan
    python3 addons.py set GnomeWorks local:.
    python3 addons.py set SomeAddon github:owner/repo
    python3 addons.py update

This exists because the addon managers for this realm either cannot be pointed
at an arbitrary repo or phone home about what you install. It does neither: it
talks to exactly the hosts you name in the manifest and to nothing else, and it
has no analytics of any kind. `check` and `update` hit api.github.com only for
the addons you have actually bound to a GitHub repo.

Stdlib only, like the converter -- no pip install, nothing to keep updated.

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
  unmanaged           leave it alone (the default for anything scan finds and
                      cannot place)

The manifest lives outside the repo, in
$XDG_CONFIG_HOME/wow-addons/manifest.json, because it holds your disk
paths and this repo is public.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wow-addons"
MANIFEST = CONFIG_DIR / "manifest.json"
USER_AGENT = "ascension-addons-sync (stdlib urllib)"

# ── output ───────────────────────────────────────────────────────────────────
# Same shape as the converter's deploy script, so output from the two reads the
# same way when you have both scrolling past.

BOLD, YELLOW, RED, DIM, RESET = "\033[1m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = YELLOW = RED = DIM = RESET = ""


def step(msg: str) -> None:
    print(f"\n{BOLD}== {msg}{RESET}")


def note(msg: str = "") -> None:
    print(f"   {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}   ! {msg}{RESET}", file=sys.stderr)


class Fail(Exception):
    """A problem worth stopping for.

    It carries its message rather than printing it, because whether it is fatal
    depends on the caller: for most commands it ends the run, but inside
    `update` it is one addon failing out of many and the rest must still go.
    Printing at the raise site would double up with the per-addon report.
    """


def die(msg: str) -> "NoReturn":  # noqa: F821
    raise Fail(msg)


# ── manifest ─────────────────────────────────────────────────────────────────


def load() -> dict:
    if not MANIFEST.exists():
        return {"addons_dir": None, "addons": {}}
    try:
        return json.loads(MANIFEST.read_text())
    except json.JSONDecodeError as exc:
        die(f"manifest is not valid JSON ({exc}). Fix or delete {MANIFEST}")


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
            "is_link": child.is_symlink(),
            "link_target": os.readlink(child) if child.is_symlink() else None,
        }
    return found


# ── sources ──────────────────────────────────────────────────────────────────


def parse_source(source: str) -> tuple[str, str]:
    """'github:owner/repo@branch' -> ('github', 'owner/repo@branch')."""
    if source == "unmanaged":
        return "unmanaged", ""
    if ":" not in source:
        die(
            f"cannot read source '{source}'. Expected one of:\n"
            "     local:/path/to/folder\n"
            "     github:owner/repo\n"
            "     github:owner/repo@branch\n"
            "     unmanaged"
        )
    kind, rest = source.split(":", 1)
    if kind not in ("local", "github"):
        die(f"unknown source type '{kind}' (expected local, github or unmanaged)")
    return kind, rest


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


def latest_github(repo_spec: str) -> tuple[str, str]:
    """(version, zip url) for the newest thing at owner/repo[@branch]."""
    if "@" in repo_spec:
        repo, branch = repo_spec.split("@", 1)
        commits = http_json(f"https://api.github.com/repos/{repo}/commits/{branch}")
        if not commits:
            die(f"no branch '{branch}' in {repo}")
        return commits["sha"][:12], f"https://api.github.com/repos/{repo}/zipball/{branch}"

    release = http_json(f"https://api.github.com/repos/{repo_spec}/releases/latest")
    if release:
        # Prefer an attached .zip: that is the packaged addon, laid out the way
        # it should sit in AddOns. The source archive is the fallback and needs
        # its wrapper directory stripped, which install_zip handles.
        for asset in release.get("assets", []):
            if asset["name"].lower().endswith(".zip"):
                return release["tag_name"], asset["browser_download_url"]
        return release["tag_name"], release["zipball_url"]

    info = http_json(f"https://api.github.com/repos/{repo_spec}")
    if not info:
        die(f"no such repo, or it is private: {repo_spec}")
    branch = info.get("default_branch", "master")
    commits = http_json(f"https://api.github.com/repos/{repo_spec}/commits/{branch}")
    sha = commits["sha"][:12] if commits else branch
    return sha, f"https://api.github.com/repos/{repo_spec}/zipball/{branch}"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


# ── installing ───────────────────────────────────────────────────────────────


def addon_dirs_in(tree: Path) -> list[tuple[Path, str]]:
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

    subdirs = [child for child in tree.iterdir() if child.is_dir()]
    if len(subdirs) == 1:
        return addon_dirs_in(subdirs[0])
    return []


def install_zip(blob: bytes, target: Path, dry_run: bool) -> list[str]:
    """Unpack an addon archive into AddOns. Returns the folder names written."""
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

        folders = addon_dirs_in(tmpdir)
        if not folders:
            die("no addon folder (a directory holding its own .toc) found in the archive")

        written = []
        for folder, name in folders:
            if "/" in name or name in ("", ".", ".."):
                die(f"archive names an unusable addon folder: {name!r}")
            destination = target / name
            if dry_run:
                note(f"would install {name}")
            else:
                if destination.is_symlink():
                    destination.unlink()
                elif destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(folder, destination)
            written.append(name)
        return written


def install_local(source_path: Path, name: str, target: Path, mode: str, dry_run: bool) -> list[str]:
    """Link (or copy) a folder from this disk into AddOns."""
    if not source_path.is_dir():
        die(f"no such folder: {source_path}")
    if not (source_path / f"{source_path.name}.toc").is_file():
        warn(f"{source_path} has no {source_path.name}.toc -- installing anyway")

    destination = target / source_path.name
    if dry_run:
        note(f"would {mode} {source_path} -> {destination}")
        return [source_path.name]

    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        # A real folder here is almost always an older manual copy. Move it
        # aside rather than delete it: this is the one step that cannot be undone.
        backup = destination.with_name(destination.name + ".replaced")
        counter = 1
        while backup.exists():
            counter += 1
            backup = destination.with_name(f"{destination.name}.replaced{counter}")
        note(f"moving existing folder aside -> {backup.name}")
        destination.rename(backup)

    if mode == "copy":
        shutil.copytree(source_path, destination)
    else:
        destination.symlink_to(source_path.resolve(), target_is_directory=True)
    return [source_path.name]


# ── commands ─────────────────────────────────────────────────────────────────


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


def cmd_init(args, state: dict) -> None:
    target = find_addons_dir(Path(args.path))
    state["addons_dir"] = str(target)
    save(state)
    step("WoW folder set")
    note(str(target))
    note("")
    note("Next:  addons.py scan     (read what is already installed)")


def cmd_scan(args, state: dict) -> None:
    root = addons_dir(state)
    installed = scan_installed(root)
    entries = state.setdefault("addons", {})

    step(f"Scanning {root}")
    guessed = 0
    for name, facts in installed.items():
        entry = entries.setdefault(name, {"source": "unmanaged", "mode": "link", "installed": None, "folders": [name]})
        entry["title"] = facts["title"]
        entry["toc_version"] = facts["version"]

        # A folder that is already a symlink is being managed by hand; record
        # where it points so `list` tells the truth instead of saying unmanaged.
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

    save(state)
    note(f"{len(installed)} addon folder(s) installed")
    if guessed:
        note(f"{guessed} with a source found or suggested -- see `addons.py list`")
    note("")
    note("Bind one with:  addons.py set <Addon> github:owner/repo")
    note("            or: addons.py set <Addon> local:/path/to/folder")


def cmd_list(args, state: dict) -> None:
    root = addons_dir(state)
    entries = state.get("addons", {})
    if not entries:
        die("nothing scanned yet. Run:  addons.py scan")

    step(f"{len(entries)} addon(s) in {root}")
    width = max(len(n) for n in entries)
    for name in sorted(entries, key=str.lower):
        entry = entries[name]
        source = entry.get("source", "unmanaged")
        installed = entry.get("installed") or entry.get("toc_version") or ""
        flags = []
        if entry.get("missing"):
            flags.append("NOT INSTALLED")
        if source == "unmanaged" and entry.get("suggested"):
            flags.append(f"suggested: {entry['suggested']}")
        tail = f"  {DIM}({', '.join(flags)}){RESET}" if flags else ""
        print(f"   {name:<{width}}  {tilde(source):<44} {installed}{tail}")


def cmd_set(args, state: dict) -> None:
    entries = state.setdefault("addons", {})
    kind, rest = parse_source(args.source)

    local_path = None
    if kind == "local":
        # `local:.` is the common case -- this repo, from wherever you ran it.
        local_path = Path(rest).expanduser().resolve()
        if local_path.is_dir() and not (local_path / f"{local_path.name}.toc").is_file():
            # Pointed at a repo root rather than an addon folder: look inside.
            candidate = local_path / args.addon
            if (candidate / f"{args.addon}.toc").is_file():
                local_path = candidate
            else:
                die(f"{local_path} holds no {args.addon}/{args.addon}.toc")
        source = f"local:{local_path}"
    else:
        source = args.source

    entry = entries.setdefault(args.addon, {"mode": "link", "installed": None, "folders": [args.addon]})
    entry["source"] = source
    entry["mode"] = "copy" if args.copy else entry.get("mode", "link")
    entry.pop("suggested", None)
    save(state)

    step(f"{args.addon} -> {tilde(source)}")
    # The folder's own name is what lands in AddOns, not the name you typed --
    # the client matches folder to .toc, so renaming on the way in would break it.
    if local_path is not None and local_path.name != args.addon:
        warn(f"that folder is named {local_path.name}, so it installs as that, not as {args.addon}.")
    if kind == "local" and entry["mode"] == "link":
        note("linked, so `git pull` in that folder is the whole update.")
    note("Run:  addons.py update " + args.addon)


def cmd_accept(args, state: dict) -> None:
    """Take every source `scan` suggested, in one go."""
    entries = state.get("addons", {})
    taken = 0
    step("Accepting suggested sources")
    for name, entry in sorted(entries.items()):
        if entry.get("source") == "unmanaged" and entry.get("suggested"):
            entry["source"] = entry.pop("suggested")
            note(f"{name} -> {entry['source']}")
            taken += 1
    save(state)
    note(f"{taken} bound" if taken else "nothing was suggested")


def cmd_update(args, state: dict) -> None:
    root = addons_dir(state)
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
        source = entry.get("source", "unmanaged")
        if source == "unmanaged":
            skipped += 1
            continue

        kind, rest = parse_source(source)
        try:
            if kind == "local":
                # Nothing to fetch: a link is already live, and a copy is
                # refreshed straight from disk.
                folders = install_local(Path(rest), name, root, entry.get("mode", "link"), args.dry_run)
                entry["folders"] = folders
                entry["installed"] = "linked" if entry.get("mode", "link") == "link" else "copied"
                note(f"{name}: {entry.get('mode', 'link')}ed from {rest}")
                changed += 1
            else:
                version, url = latest_github(rest)
                if entry.get("installed") == version and not args.force:
                    note(f"{DIM}{name}: up to date ({version}){RESET}")
                    skipped += 1
                    continue
                note(f"{name}: {entry.get('installed') or 'not installed'} -> {version}")
                if not args.check:
                    folders = install_zip(download(url), root, args.dry_run)
                    entry["folders"] = folders
                    if not args.dry_run:
                        entry["installed"] = version
                        entry.pop("missing", None)
                changed += 1
        except Exception as exc:
            # One unreachable, private or renamed repo must not abort the run:
            # letting it through would also drop every manifest change made
            # before it, so a failure late in the list would undo the successes
            # ahead of it.
            warn(f"{name}: {exc}" if str(exc) else f"{name}: failed")
            failed.append(name)

    if not args.dry_run and not args.check:
        save(state)
    step(f"Done — {changed} changed, {skipped} unchanged/unmanaged, {len(failed)} failed")
    if changed:
        note("Restart the client, or /reload, to pick the changes up.")
    if failed:
        note(f"failed: {', '.join(failed)}")
        raise SystemExit(1)


def cmd_where(args, state: dict) -> None:
    note(f"manifest:  {MANIFEST}")
    note(f"AddOns:    {state.get('addons_dir') or '(not set)'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="addons.py",
        description="Bind each installed WoW addon to the repo you want it updated from.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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

    args = parser.parse_args()
    try:
        args.func(args, load())
    except Fail as exc:
        print(f"{RED}\nFAILED: {exc}{RESET}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
