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
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wow-addons"
MANIFEST = CONFIG_DIR / "manifest.json"
USER_AGENT = "wow-addons-sync (stdlib urllib)"


class Fail(Exception):
    """A problem worth stopping for. Carries its message; never prints it."""


def die(msg: str) -> "NoReturn":  # noqa: F821
    raise Fail(msg)


def _nothing(*_args) -> None:
    """The default `progress`/`report`: accept anything, do nothing."""


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


def new_entry(name: str) -> dict:
    return {"source": "unmanaged", "mode": "link", "installed": None, "folders": [name]}


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


def set_source(state: dict, addon: str, source: str, *, copy: bool = False) -> tuple[dict, Path | None]:
    """Bind one addon to a source. Returns (entry, local_path)."""
    entries = state.setdefault("addons", {})
    source, kind, local_path = resolve_source(addon, source)

    entry = entries.setdefault(addon, new_entry(addon))
    entry["source"] = source
    entry["mode"] = "copy" if copy else entry.get("mode", "link")
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
    """Unpack an addon archive into AddOns. Returns the folder names written.

    Under `dry_run` nothing is written but the names are still returned, so a
    caller can report exactly what it would have installed without this needing
    to know how that report is displayed.
    """
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
            if not dry_run:
                if destination.is_symlink():
                    destination.unlink()
                elif destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(folder, destination)
            written.append(name)
        return written


def install_local(source_path: Path, target: Path, mode: str, dry_run: bool, *, report=None) -> list[str]:
    """Link (or copy) a folder from this disk into AddOns."""
    report = report or _nothing
    if not source_path.is_dir():
        die(f"no such folder: {source_path}")
    if not (source_path / f"{source_path.name}.toc").is_file():
        report("warn", f"{source_path} has no {source_path.name}.toc -- installing anyway")

    destination = target / source_path.name
    if dry_run:
        return [source_path.name]

    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        # A real folder here is almost always an older manual copy. Move it
        # aside rather than delete it: this is the one step that cannot be undone.
        backup = backup_name(destination)
        report("note", f"moving existing folder aside -> {backup.name}")
        destination.rename(backup)

    if mode == "copy":
        shutil.copytree(source_path, destination)
    else:
        destination.symlink_to(source_path.resolve(), target_is_directory=True)
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


def will_displace(entry: dict, root: Path) -> Path | None:
    """The real folder that binding this entry would move aside, if any.

    A symlink is replaced silently -- it holds no files of its own. A real
    directory is somebody's manual install and moving it is the one step in
    this tool that cannot be undone.
    """
    source = entry.get("source", "unmanaged")
    if not source.startswith("local:"):
        return None
    destination = root / Path(source[len("local:"):]).name
    if destination.exists() and not destination.is_symlink():
        return backup_name(destination)
    return None


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
            result.folders = install_local(Path(rest), root, mode, dry_run, report=report)
            result.version = "linked" if mode == "link" else "copied"
            result.detail = (f"would {mode} from {rest}" if dry_run else f"{mode}ed from {rest}")
            if not dry_run:
                entry["folders"] = result.folders
                entry["installed"] = result.version
                entry.pop("missing", None)
            return result

        progress("checking", rest)
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
        result.folders = install_zip(blob, root, dry_run)
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
