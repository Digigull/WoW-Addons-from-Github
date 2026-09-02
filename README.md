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
python3 addons.py install someone/NewAddon                 # one you do not have yet
python3 addons.py set MyAddon github:someone/MyAddon       # one you already have
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
| `scan` | Read every addon already installed and record it, guessing a source from each `.toc` where it can. Drops unmanaged rows whose folder you have deleted; keeps bound ones, flagged *not installed*. |
| `install <repo>` | Fetch an addon you do **not** have yet and bind it in one step. Takes `owner/repo`, `owner/repo#Folder` or any github.com link; `--folder` (repeatable) picks addons out of a repository that holds several — or the `.toc` out of one that ships several, `--folder NotPlater-3.3.5.toc` — and `--branch` follows a branch instead of releases. `--reset-settings` also deletes that addon's saved variables (account and every character) once it has installed, keeping a copy of each unless you add `--no-settings-backup`. |
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

### A repository that is one addon with a `.toc` per client

[RichSteini/NotPlater](https://github.com/RichSteini/NotPlater) is one addon in one root
holding `NotPlater-2.4.3.toc` and `NotPlater-3.3.5.toc` — TBC and Wrath, same files. There
is no `NotPlater.toc` between them, and 3.3.5 has no notion of flavour `.toc`s at all: it
loads `<Folder>/<Folder>.toc` and nothing else. So the folder in `AddOns` has to be **named
after the one you want**, which makes this a question only you can answer — it is which
client you play.

Both dialogs offer the `.toc` files as tick boxes and **refuse to proceed with none
ticked**; there is no "install all of them", because all of them would be this one addon
installed twice over under names only one of which your client loads. Tick both anyway if
you actually want both folders. The source records the choice:

```
addons.py install RichSteini/NotPlater --folder NotPlater-3.3.5.toc
github:RichSteini/NotPlater#NotPlater-3.3.5.toc
```

That row still follows the repository's own releases — there is no folder to date, the
whole repository is the addon.

This is **not** the same as `FrostSeek.toc` beside `FrostSeek_Wrath.toc` and five more.
There a base `.toc` exists that every other extends, which is the convention the client
itself resolves out of a single folder named `FrostSeek`; splitting those would break all
of them. Nothing is asked in that case, and nothing should be.

A folder in `AddOns` is an addon when it holds a `.toc` named after itself — the rule the
game uses, matched the way the game matches it. That match ignores case, so
`PlayerbotManager/Playerbotmanager.toc` is an addon here exactly as it is in the client,
where Windows and Wine both find it. A folder that plainly holds an addon and still will not
load — the `.toc` named after something else, the addon left one level down inside the folder
its zip made, a `.toc.txt` saved by an Explorer that hides extensions — is named with its fix
rather than silently dropped: a folder you can see in `AddOns` and cannot see in the list
makes the scan look broken.

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
itself — including a repository that is the addon and keeps its libraries beside it, where
everything under the root belongs to the one addon at the root.

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
- **Install addon…** takes a repository — `owner/repo` or any github.com link — and
  fetches an addon that is not in your AddOns folder at all yet. It reads what the
  repository holds and, if that is several addons, asks which; each one you tick becomes
  its own row and its own binding, rather than one row that reinstalls all of them
  whenever any one of them changes. A repository holding a single addon is bound whole,
  which keeps it following that repository's releases.
- **Installing over an addon you already have asks first**, in a window that
  keeps two very different questions apart. *Make a backup* — ticked — moves the folder
  that is there now to `<Name>.replaced` instead of deleting it. Below a rule, under a red
  **Delete!** heading, is the part that is not undoable: **delete the associated saved
  variables** — off by default, and it names every file it means, account-wide and one per
  character, so you can see the extent of it before agreeing. Ticking it enables *back up
  the saved variables first*, which is itself ticked: those are settings you made, and no
  repository anywhere has a copy. They are deleted **after** the addon installs, so a
  download that fails takes nothing with it.
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
- Binding **or installing** over an addon that exists as real files says so in the
  dialog, and names the `<Name>.replaced` folder it would move them to, *before* you
  click Save or Install.

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
- **It never touches your saved variables unless you tick the box that says so.**
  Updating an addon leaves `WTF/Account/…/SavedVariables/<Addon>.lua` exactly where it is —
  your bars stay where you put them. The only thing that deletes one is the **Delete!**
  section of the install confirmation, or `install --reset-settings`, and both keep a
  `.replaced` copy beside each file unless you turn that off too.
- **A rescan never throws away a binding.** An unmanaged row whose folder you deleted is
  dropped from the list, because it held nothing of yours. A row you had bound is kept and
  flagged *not installed* — that is also how an addon you have bound but not yet fetched
  appears, and the binding is the one thing scanning cannot work out again. Set its source
  to `unmanaged` and rescan to be rid of it.
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

Most of a run no longer touches the API at all:

- **The questions git can answer go to git.** Before a clone, git asks a server to list
  what it has — `github.com/owner/repo.git/info/refs`, the request every `git fetch`
  begins with. It is not the REST API and is not billed against the hourly quota, and one
  of them per repository yields the default branch, every branch and every tag with the
  commit each points at. That is the default-branch lookup and the branch-head lookup
  gone, and — since a published release always has a tag — a repository with no tags is
  never asked about releases at all.
- **History is only asked for when something moved.** Which commit last touched a folder
  is not in a ref listing, but a folder cannot change unless the branch holding it moves.
  Ten addons in one unmoved monorepo cost nothing.
- **A check that finds nothing new is free.** Whatever is left is stored with the `ETag`
  GitHub stamped on it and sent back as `If-None-Match`; an unchanged resource replies
  `304 Not Modified`, which GitHub does not bill.
- **Archives are fetched off the meter.** A zip comes from `codeload.github.com`, the host
  behind the green *Download ZIP* button, which is not the REST API. The REST URL stays as
  an automatic fallback.
- **The wall is not re-hit.** Once the quota is known to be spent the run fails once,
  immediately, naming the time it comes back, rather than spending a round trip per
  remaining addon to be told the same thing.

What that costs in API calls, per addon, for a full update where the addon really changed:

| Source | First run | Every run after |
|---|---|---|
| `local:/path/to/checkout` | **0** | **0** |
| `github:owner/repo` — repo publishes **no** releases | **0** | **0** |
| `github:owner/repo@main` — branch pinned | **0** | **0** |
| `github:owner/repo` — repo publishes releases | 1 | **0** |
| `github:owner/repo#Folder` — folder pinned | 1 | **0** |

Nothing is taken on trust anywhere in that. A ref that cannot be seen is never reported as
unchanged, an unreadable listing is ignored rather than guessed at, and a private
repository or a network that blocks git falls straight back to the REST path — the same
shape as codeload falling back to the API zipball.

### Checking without the API at all

The 60 an hour is counted **per address**, so on a shared, office or CGNAT connection
something else can spend yours. For that case there is a checkbox — *Check without the
GitHub API*, or `--no-api` in the terminal — under which no API call is made under any
circumstance:

- an addon's version comes from hashing its folder inside the repository's archive, which
  comes from the same host as the *Download ZIP* button and costs no quota;
- the digest is kept against the commit it came from, so it is computed once ever, and the
  free ref listing means an archive is only fetched when the branch has actually moved;
- an addon bound to a whole repository downloads nothing at all to check — the ref listing
  already carries the commit.

**It cannot see releases**, and that is why it is a checkbox rather than the default. A
release asset is a file the author *uploaded*; it is not in the repository, so no amount
of downloading the repository will find it. Addons checked this way follow their default
branch and install the source tree instead of the author's packaged zip — which for an
addon whose author ships a properly packaged build is a real downgrade, not just a slower
route. Bandwidth replaces quota. Leave it off unless you are actually hitting the limit.

The setting is per install, so a server behind a shared address and one at home need not
agree about it.

**No repository is kept on your disk.** The archive is held in memory for the length of
the run and discarded; installing unpacks it into the system temporary directory, which is
emptied as soon as the addon is in place. The only thing that persists is
`github-cache.json` beside the manifest, holding ETags and twelve-character digests — no
archives. It is capped at 400 entries, and deleting it costs one cold run and nothing
else.

| | Temporary, during an install | Kept between runs |
|---|---|---|
| **Linux** | `$TMPDIR`, else `/tmp` | `~/.config/wow-addons/github-cache.json` |
| **macOS** | `$TMPDIR` (`/var/folders/…/T/`) | `~/.config/wow-addons/github-cache.json` |
| **Windows** | `%TMP%`, normally `…\AppData\Local\Temp` | `%APPDATA%\wow-addons\github-cache.json` |

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

**First, in the commit that finishes the work:** bump `__version__` in
`wowaddons/__init__.py` and add a `## New in vX.Y.Z` section to
`.github/release-notes.md`. The tag you build must match the version the code reports —
the `version-matches-tag` job refuses the release otherwise, because a binary that lies
about which build it is makes every bug report start from nothing. A test checks the notes
and the version agree on every push, so forgetting is caught long before you get here.

Then, entirely from the browser, no git checkout needed. **Actions → release → Run
workflow**, leave the branch on `main`, then:

| | Tag | Build from |
|---|---|---|
| **a new release** | `v0.5.0` | `main` |
| **rebuild one whose build failed** | the existing tag | *(blank)* |
| **just check the builds** | *(blank)* | *(blank)* |

A new tag is created at the commit that was actually built, and the release gets its title,
notes and both binaries. Rebuilding edits the existing release in place —
same tag, same URL, nothing duplicated — which is what makes a failed release repairable
without moving a tag, something the web UI cannot do.

