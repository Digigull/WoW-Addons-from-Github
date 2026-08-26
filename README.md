# WoW Addons from GitHub

Install and update World of Warcraft addons from whatever repositories **you** choose.

Point it at your WoW folder, let it scan what is already installed, bind each addon to
where its updates should come from, and update.

There are two ways to use it, over the same engine — a window, or the terminal.

### The window (Linux)

Download the AppImage from [the latest release](../../releases/latest), make it executable,
and open it.

```
chmod +x WoW-Addons-from-GitHub-x86_64.AppImage
./WoW-Addons-from-GitHub-x86_64.AppImage
```

About 20 MB, and that is everything: its own Python, its own Tk, and the tool. No install,
nothing to keep updated, and deleting the file uninstalls it. It asks for your WoW folder
the first time, then shows every addon it found with the source bound to each one.

It runs on anything with glibc 2.17 or newer — CentOS 7 era, and older than anything
anyone is realistically running.

> **If it will not start** with an error mentioning `libfuse.so.2`, your distribution no
> longer ships FUSE 2 — recent Debian and Ubuntu do not. Either install it
> (`sudo apt install libfuse2`, or `libfuse2t64` on newer Debian and Ubuntu), or skip it
> entirely with `./WoW-Addons-from-GitHub-x86_64.AppImage --appimage-extract-and-run`.
> That is the whole problem; nothing else about the AppImage needs anything installed.

### The window (Windows)

Download the zip from [the latest release](../../releases/latest), unzip it anywhere, and run
**WoW Addons from GitHub.exe** inside.

No Python, no installer, no registry entries — delete the folder and it is gone. The same
`.exe` is also the command line: give it arguments and it behaves exactly like the script
below.

> **Windows will warn you the first time.** The download is not code-signed, so SmartScreen
> shows "Windows protected your PC". Click **More info → Run anyway**. A signing certificate
> is the only thing that removes that, and it costs a few hundred dollars a year — not worth
> it until enough people are using this to justify it.

Not sure which build you have? `--version` says, and the window puts it in its title bar.

### The terminal

```
python3 addons.py init ~/Games/Ascension
python3 addons.py scan
python3 addons.py set MyAddon github:someone/MyAddon
python3 addons.py update
```

The CLI stays first-class and is not going anywhere. Both downloads are the same tool:
give either one arguments and it behaves exactly like the script above.

```
./WoW-Addons-from-GitHub-x86_64.AppImage update --check
"WoW Addons from GitHub.exe" update --check
```

## Why

The addon managers for private realms tend to have two problems: the catalogue is theirs, so
you cannot point an addon at an arbitrary repository, and some of them report what you
install whether or not you opted in.

This does neither. There is no catalogue, no account and **no telemetry of any kind** — it
contacts exactly the hosts named in your own manifest and nothing else. `update` reaches
`api.github.com` only for addons you have actually bound to a GitHub repository, and reaches
nothing at all for addons you keep on local disk.

It uses nothing but the Python standard library — no dependencies, in the tool or in the
window. That is why the AppImage is one self-contained file you delete when you are done
with it, and why the window is Tkinter rather than something prettier that would have put
a package manager between you and a working program.

## Requirements

**The AppImage needs nothing.** It carries its own Python and its own Tk. FUSE 2 at
runtime is the only caveat, and `--appimage-extract-and-run` sidesteps even that.

Running from a checkout instead needs **Python 3.9 or newer**, and that is the whole list.

- **Linux:** already present on essentially every distribution. For the window you may also
  need Tk, which some distributions package separately: `sudo apt install python3-tk`,
  `sudo dnf install python3-tkinter`, or `sudo pacman -S tk`. Running the tool with no
  arguments tells you which of these you want if it is missing.
- **Windows:** `winget install Python.Python.3.12`, or python.org — Tk is included. The
  packaged `.exe` removes this step entirely.

## Commands

| Command | What it does |
|---|---|
| `init <path>` | Remember where the client is. Accepts your WoW folder, its `Interface` folder, or `Interface/AddOns` itself. |
| `scan` | Read every addon already installed and record it, guessing a source from each `.toc` where it can. |
| `list` | Every addon, its source, and its installed version. Bound addons first, then the rest, each alphabetically. |
| `set <Addon> <source>` | Bind one addon to where its updates come from. |
| `accept` | Take every source that `scan` suggested, in one go. |
| `update [Addon...]` | Bring bound addons up to date. Defaults to all of them. |
| `where` | Print the manifest and AddOns paths. |

`--version` prints the version and exits; the window shows it in the title bar.

Useful flags on `update`: `--check` (report what is out of date, download nothing),
`--dry-run`, `--force` (reinstall even when the version matches). In the window, **Check
for updates** does the same thing and fills in the **Latest** column without downloading.

Flags on `set`: `--copy` (real files instead of a link), `--no-backup` (replace an
existing folder outright instead of keeping one copy of it).

## Several WoW folders

A vanilla server, a Wrath one and retail are separate **installs**: separate AddOns
directories, separate bindings, nothing shared between them. Running `init` again adds one
rather than replacing the first, and it takes its name from the folder unless you give one.

```
addons.py init ~/Games/Vanilla --name Vanilla
addons.py installs                    # what is known; * marks the one in use
addons.py use Vanilla                 # switch
addons.py update --install Wrath      # aim one run elsewhere, without switching
addons.py forget Vanilla              # stop tracking it — deletes no game files
```

In the window, a picker appears above the table once there is more than one; it is hidden
while there is only one, because a dropdown with a single entry can do nothing.

The same addon can be bound differently in each — a different branch, or a different folder
of the same repository — which is usually the reason for having two. `--install` aims a
single run at another folder without changing which one later commands use.

An existing manifest from before this becomes a single install named after its WoW folder,
the first time the tool reads it. Nothing is rewritten until something saves.

## Sources

| Source | Behaviour |
|---|---|
| `local:<path>` | A folder on disk. Installed as a **symlink** by default, so `git pull` in that checkout *is* the update — nothing to reinstall, and the client cannot be running something other than what is checked out. `--copy` for real files instead. |
| `github:owner/repo` | Latest release, preferring an attached `.zip` and falling back to the source archive. |
| `github:owner/repo@branch` | That branch's current head, for an addon that does not cut releases. |
| `github:owner/repo#Folder` | **One addon out of a repository that holds several.** Only that folder is installed, and its version is the last commit that touched *it*. Combines with a branch: `github:owner/repo@main#Folder`. |
| `https://github.com/owner/repo` | A pasted link works too — the page URL, the clone URL, the SSH one, a link to a branch (taken as `@branch`), or a link to a folder (taken as `#Folder`). |
| `unmanaged` | Left alone. The default for anything `scan` finds and cannot place. |

A link to a GitHub **account or organisation** (`github.com/Some-Org`) names no repository,
so it is refused with a note saying so — open the addon you want and paste that address.

### A repository that holds several addons

Some people keep every addon they have written in one repository. Bound as a whole, such a
repo installs **all** of it, and since the repo has one commit history, every addon in it
reports an update whenever any one of them changes.

**The window lists them for you.** Type or paste the repository into Set source and the
addons it holds appear underneath as tick boxes, with the one matching this row already
ticked. Tick more than one if an addon and its companion belong together — they become a
single row that updates as a unit. Saving with nothing ticked asks first, because that
binds the whole repository.

Nothing is guessed when nothing matches: a wrong guess arriving pre-ticked would be
accepted without being read. And a repository holding a single addon offers no tick boxes —
one candidate is not a choice, and naming it would switch that addon from its releases to
commit ids for no gain.

A row already bound to a whole multi-addon repository is flagged in the table as
*installs N addons*, so it can be narrowed whenever it suits.

You can also name the folder yourself, which is what the tick boxes write for you. Clicking
into an addon on github.com and pasting that address does the same thing:

```
https://github.com/owner/repo/tree/main/MyAddon
```

The window fills in the repository, the branch and the folder from that link. Each addon in
the repo can be bound separately, and each then updates — and reports updates — on its own.

Bind the repository as a whole and the tool says so once, in the run log, rather than
leaving it to be discovered as strange behaviour later.

`scan` reads each `.toc` for an `X-Website` or `X-Repository` header and *suggests* a GitHub
source where it finds one. Suggestions are never applied on their own — a header is the
author's claim about where the code lives, which is not the same as your decision to install
from there. `accept` applies them all once you have looked.

Archives are unpacked whatever shape they arrive in: a packaged release
(`MyAddon/MyAddon.toc`), GitHub's source archive (`repo-1a2b3c/MyAddon/MyAddon.toc`), and a
repository whose root *is* the addon (`repo-1a2b3c/MyAddon.toc`) all end up correctly as
`AddOns/MyAddon/MyAddon.toc`.

> Pointing a `github:` source at a repository containing **several** addon folders installs
> all of them — which is right for an addon that ships its own library, and wrong for a
> repository of unrelated addons. Add `#Folder` to take just one; see
> [A repository that holds several addons](#a-repository-that-holds-several-addons).

Addons are found wherever they sit in the archive: at the top, under GitHub's wrapper
directory, laid out as `src/MyAddon/MyAddon.toc` beside a `docs/` folder, or as a repository
whose root *is* the addon. An addon's own bundled libraries are never mistaken for the addon
itself.

## Working on your own addon

If you write addons, the loop you want is not download-a-zip-and-copy-folders. There are
two shapes, depending on where you edit.

### You edit on the same machine the client runs on

Bind the addon to your checkout. This is the cheapest thing the tool does — **no GitHub
calls, no downloads, nothing to press between edits**:

```
git clone https://github.com/you/my-addons ~/src/my-addons     # once
addons.py set HonorTracker local:~/src/my-addons/HonorTracker
addons.py update HonorTracker
```

By default that installs a **symlink**, so `Interface/AddOns/HonorTracker` *is* your
working tree. Save a file, `/reload` in the client, and you are looking at the change.
There is no second update step, and the client cannot be running something other than what
is checked out. `git pull` in the checkout is the whole update.

Point the source at the **checkout root** and name the addon, and it finds the folder
itself — handy when one repository holds several:

```
addons.py set HonorTracker local:~/src/my-addons     # finds HonorTracker/HonorTracker.toc
addons.py set LootLog      local:~/src/my-addons
```

In the window it is the same thing: **Set source…**, *A folder on disk*, Browse.

If you would rather have real files than a link — to check what a user's install actually
looks like, or because something in your toolchain dislikes links — add `--copy`, or tick
the box in the dialog. Then `update` copies the folder each time, and you do press Update
after an edit.

### You edit somewhere else and push to GitHub

Then you do **not** need a clone, a zip, or a git command on the WoW machine. Bind the row
to the repository once and press Update whenever you want the pushed version:

```
addons.py set HonorTracker github:you/my-addons#HonorTracker
addons.py update HonorTracker
```

Fetching and unpacking the archive and replacing the folder is exactly what this tool is
for; downloading the zip by hand and copying folders into `AddOns` is the job it removes.
Naming the folder with `#HonorTracker` matters in a repository that holds several addons —
without it the row installs all of them, and every one of them reports an update whenever
any one of them changes.

After the first fetch this is nearly free too: an unchanged repo answers `304`, which
GitHub does not bill, and the archive comes from a host that is not the API. See
[GitHub's rate limit](#githubs-rate-limit).

### Going back and forth

Switch a row between the two whenever it suits — `local:` while you are working on it,
`github:` when you want to see what somebody else would actually receive:

```
addons.py set HonorTracker github:you/my-addons#HonorTracker   # test the pushed version
addons.py set HonorTracker local:~/src/my-addons               # back to the checkout
```

Two things worth knowing before the first bind:

- **If a real folder is already there, it is moved aside, not deleted** — to
  `HonorTracker.replaced`, once, and later updates leave that first copy alone. The window
  names the folder it would create *before* you click Save.
- **On Windows a `local:` source installs as a directory junction**, not a symlink, so it
  needs neither administrator rights nor Developer Mode. The client cannot tell the
  difference.

## The window

![the window](docs/window.png)

Everything the terminal does, in one window:

- **The addons you have bound are listed first**, then everything still unmanaged, each
  group alphabetically. On a real install most rows are addons this tool does not manage,
  and a single alphabetical list buries the handful it does among them.
- **Set source…** opens a dialog over the selected addon: a local folder (with Browse),
  a GitHub repo, an optional branch to track, an optional folder inside the repo, or
  unmanaged. Pasting a github.com link to a folder fills all three in.
- **Accept suggestion** takes what an addon's `.toc` suggested — for the rows you pick,
  on a click you make. It is shown in the Status column and never applied on its own,
  for the same reason `accept` is a separate command in the terminal.
- **Check for updates** fills the **Latest** column and downloads nothing, so seeing what
  is out of date does not commit you to installing it.
- **Update selected** / **Update all** run in the background, so the window stays
  responsive while things download. **Stop** ends the run after the addon in flight
  rather than mid-download, which leaves the manifest agreeing with the disk.
- A repository that cannot be reached marks **that row** red and leaves the rest to
  finish. There is no error dialog to dismiss per failure.
- Binding an addon that exists as real files says so in the dialog, and names the
  `<Name>.replaced` folder it would move them to, *before* you click Save.

## Things it will not do to your client

- **It keeps the first copy of anything you installed yourself.** Binding an addon that
  exists as real files moves that folder to `<Name>.replaced`, in your AddOns folder,
  beside the addon. Nothing inside it matches that name, so the client ignores it; delete
  it yourself once you are satisfied.

  **Once, not every update.** After the first install the folder is one this tool wrote,
  so later updates replace it directly — no `<Name>.replaced2` piling up. The question is
  asked **per folder**: an archive landing several folders keeps each one that this tool did
  not write, whatever it recorded about the addon you bound. Turn the copy
  off entirely with `--no-backup`, or the checkbox in the Set-source dialog, and an
  existing folder is replaced outright.
- **One failed source does not sink the run.** An unreachable, private or renamed repository
  is reported and skipped — everything else still updates, and the manifest still saves.
- **Archives containing `../` paths are refused.** This unpacks zips published by third
  parties.
- **Nothing but the manifest and a cache is written outside AddOns.** `github-cache.json`
  sits beside the manifest, holds only what GitHub already told us, and is safe to delete.
- **Your manifest stays out of the way**, at `$XDG_CONFIG_HOME/wow-addons/manifest.json` —
  in practice `~/.config/wow-addons/manifest.json`, and `%APPDATA%\wow-addons\` on Windows.
  It holds your disk paths, so it does not belong in a repository. `where` prints the
  resolved location.

Restart the client, or `/reload`, to pick changes up.

## GitHub's rate limit

Unauthenticated GitHub allows 60 API calls an hour, and separately objects to bursts
however much of that is left. Checking one addon costs a call or two and downloading it
used to cost another, so `Update all` over a longer list was a burst of them fired as
fast as the network answered — which is how a perfectly ordinary addon list came back
**GitHub rate limit reached**, and then spent one more doomed call per remaining addon
saying so again.

Four things now keep a run inside the budget:

- **A check that finds nothing new is free.** Every answer is stored with the `ETag`
  GitHub stamped on it, and sent back as `If-None-Match` next time. An unchanged resource
  replies `304 Not Modified`, and GitHub does not bill a `304`. Check as often as you
  like, as long as your addons are not moving.
- **Archives are fetched off the meter.** A zip comes from `codeload.github.com`, the host
  behind the green *Download ZIP* button, which is not the REST API and does not spend the
  hourly quota. The REST URL stays as an automatic fallback, so a run that cannot use
  codeload still installs, for the price of a call.
- **One question per run.** Ten addons out of one repository share its branch lookup.
- **The wall is not re-hit.** Once the quota is known to be spent the run fails once,
  immediately, naming the time it comes back, rather than spending a round trip per
  remaining addon to be told the same thing.

What that costs in practice, per addon, for a full update where the addon really changed:

| Source | First run | Every run after |
|---|---|---|
| `local:/path/to/checkout` | **0** | **0** |
| `github:owner/repo` — repo publishes releases | 1 | **0** |
| `github:owner/repo@main` — branch pinned | 1 | **0** |
| `github:owner/repo@main#Folder` — branch and folder pinned | 1 | **0** |
| `github:owner/repo#Folder` — folder pinned | 2 | **0** |
| `github:owner/repo` — repo publishes **no** releases | 3 | 1 |

The last row is the only shape that keeps costing something, because "this repo has no
releases" comes back as a `404`, and a `404` carries no `ETag` to revalidate against.
Pinning the branch — `github:owner/repo@main` — skips the release lookup entirely and
takes it to zero.

Both front ends print how many calls are left after a run, and say when they are waiting
and why, so a pause never looks like a hang. What was learned is kept in
`github-cache.json` beside the manifest; deleting it costs one cold run and nothing else.

`GITHUB_TOKEN` is still honoured and still entirely optional — it raises the limit to 5000
calls an hour. With the above it is rarely the thing standing between you and a finished
update.

An addon you are writing yourself costs nothing at all — see
[Working on your own addon](#working-on-your-own-addon).

## Linux, Wine and Proton

Symlinks pass straight through to the Windows side, so the client sees an ordinary folder.
No configuration needed.

The one exception is **Flatpak Bottles**, whose sandbox cannot follow a link out to your home
directory. Grant it the checkout once:

```
flatpak override --user --filesystem=/path/to/your/checkout com.usebottles.bottles
```

Then restart Bottles. Or bind that addon with `--copy` and avoid links entirely.

## Windows

The tool runs on Windows with Python installed — the CLI, the window, and both source
types. The two things that used to be Linux-shaped are fixed:

- **`local:` sources install as a directory junction**, not a symlink. `os.symlink` on
  Windows needs administrator rights or Developer Mode; a junction needs neither, and the
  client cannot tell the difference. `--copy` still works if you would rather have real
  files.
- **The manifest lives in `%APPDATA%\wow-addons\`.** If you used an earlier version, your
  old manifest under `~/.config\wow-addons\` is read automatically and moves to the new
  location the next time anything is written — you do not need to do anything.

The packaged `.exe` is built and released alongside the AppImage. It is a **folder in a
zip**, not a single self-extracting file: one-file builds are what a lot of malware looks
like, so antivirus flags them far more often, and they unpack to a temp directory on every
launch.

One consequence of building it `--windowed` — which is what makes double-clicking open a
window instead of a console — is that running it from `cmd` with arguments borrows the
console you typed into, and `cmd` does not wait for it. Your prompt comes back first and
the output appears underneath it. That is cosmetic; redirection (`... > out.txt`) works
normally.

The full design and packaging plan is in [UI-PLAN.md](UI-PLAN.md).

## Development

```
wowaddons/core.py        the engine: manifest, scanning, sources, install. Never prints.
wowaddons/cli.py         argparse, and the only module that writes to a terminal
wowaddons/gui.py         the Tkinter window
wowaddons/winconsole.py  how a windowed .exe grows a console when given arguments
addons.py                launcher: the window with no arguments, the CLI with them
packaging/make_icon.py   the icon, for both platforms, from one description
packaging/appimage/      the AppImage recipe, build script and smoke test
packaging/windows/       the PyInstaller build and its smoke test
```

Both front ends call `core.update_addon` for one addon at a time, which is what keeps them
from drifting: the rule that one failure does not sink the run is written once, not once
per front end. Nothing in `core` prints — that is the constraint that makes a window
possible at all, and it is worth defending.

```
python3 -m unittest discover -s tests -t . -v          # 92 tests
xvfb-run -a python3 -m unittest discover -s tests -t . # including the window
```

No network and no game client: archives are built in memory, the GitHub API is stubbed,
and the window tests stub the engine and drive Tk headlessly. They run in about a second.
The window tests skip themselves where there is no display, so a checkout on a server
stays green; CI has a job that installs Tk and Xvfb on purpose and fails if they skip.

The suite is not there for coverage. It pins the things that actually broke, plus the
guards whose failure would otherwise be silent: an archive whose root is the addon
installing under GitHub's wrapper name (which the client ignores), a `403` reported as a
rate limit when the real cause was a blocked proxy or a private repository, and an
AppImage entry point that stops going through the wrapper that sets up Tcl/Tk — which
would produce a build that passes every check except opening a window, and an update run
that spends its GitHub quota in one burst and then keeps asking after it is gone.

### Building the downloads

```
packaging/appimage/build.sh                                                # Linux
packaging/appimage/smoke-test.sh dist/WoW-Addons-from-GitHub-x86_64.AppImage

python packaging/windows/build.py                                          # Windows
python packaging/windows/smoke-test.py "dist/WoW Addons from GitHub"
```

PyInstaller only ever builds for the machine it runs on, so the `.exe` comes from CI or
from a Windows box. Running `packaging/windows/build.py` from a Linux checkout is still
worth doing: it produces a Linux binary and proves the entry point resolves and that Tk
and the lazily-imported `wowaddons.gui` were actually collected — the two failures that
otherwise surface only as a `.exe` with no window.

`build.sh` puts its own tooling in `.build-venv/` and installs nothing into your Python.
The AppImage itself has no dependencies — `python-appimage` is used to wrap a manylinux
CPython that already contains Tkinter, and only the standard library and the `wowaddons`
package end up inside.

Its glibc floor comes from that bundled interpreter (manylinux2014, glibc 2.17), not from
the machine doing the building, because none of this project is compiled. The usual
AppImage advice to build on the oldest distribution you support does not apply.

The icon is drawn by `packaging/appimage/make_icon.py` using nothing but `zlib` and
`struct`, so it stays a readable diff rather than an opaque blob; a test re-runs it and
fails if the committed PNG has drifted.

## Licence

MIT — see [LICENSE](LICENSE).

## Cutting a release

Entirely from the browser, no git checkout needed. **Actions → release → Run workflow**,
leave the branch on `main`, then:

| | Tag | Build from |
|---|---|---|
| **a new release** | `v0.5.0` | `main` |
| **rebuild one whose build failed** | the existing tag | *(blank)* |
| **just check the builds** | *(blank)* | *(blank)* |

A new tag is created at the commit that was actually built, and the release gets its title,
notes, pre-release label and both binaries. Rebuilding edits the existing release in place —
same tag, same URL, nothing duplicated — which is what makes a failed release repairable
without moving a tag, something the web UI cannot do.

