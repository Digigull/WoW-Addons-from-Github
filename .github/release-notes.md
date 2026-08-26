Update your World of Warcraft addons from repositories **you** choose. No catalogue, no
account, and no telemetry of any kind — it contacts exactly the hosts named in your own
manifest and nothing else.

## New in v0.7.0

**Updating no longer runs into GitHub's rate limit.** Unauthenticated GitHub allows 60 API
calls an hour, and separately objects to bursts however much of that is left. Checking one
addon cost a call or two and downloading it cost another, all fired as fast as the network
answered — which is how an ordinary addon list came back *GitHub rate limit reached*, and
then spent one more doomed call per remaining addon saying so again.

A check that finds nothing new is now **free**. Every answer is stored with the tag GitHub
stamps on it and handed back next time; an unchanged repository replies "not modified",
which GitHub does not count. Archives are fetched from the host behind the green *Download
ZIP* button, which is not the API and does not spend the quota either. Ten addons out of
one repository: eleven calls the first time, none at all for every check after it until
something is actually pushed.

Calls are also spaced out, so a long list is not refused for arriving too fast, and a
quota that really has run out now fails the run once — telling you when it comes back —
instead of being re-hit for every addon left. Both the window and the terminal show how
many calls are left, and say when they are waiting and why.

**The addons you have bound are listed first**, then everything still unmanaged, each
group alphabetically. On a real install most rows are addons this tool does not manage,
and a single alphabetical list buried the handful it does among them.

**Working on your own addon is now documented, and costs nothing.** Bind it to your
checkout with a `local:` source and the client reads your working tree directly — save a
file, `/reload`, done. No downloads, no API calls, and nothing to press between edits. The
README has a section on it, including the case where you push to GitHub from elsewhere and
just want the pushed version installed.

## New in v0.6.0

**The window shows you what a repository holds.** Paste a repo into Set source and its
addons appear underneath as tick boxes, with the one matching that row already ticked.
No need to know what a folder path is, and no need to leave the window to find out.

Tick more than one if an addon and its companion belong together — they become one row
that updates as a unit. Save with nothing ticked and it asks first, because that binds the
whole repository and installs every addon in it.

Nothing is guessed when nothing matches. A wrong guess that arrives pre-ticked gets
accepted without being read, and the addon then updates from somebody else's folder.

A repository holding **one** addon offers no tick boxes at all — one candidate is not a
choice, and naming it would switch that addon from its published releases to commit ids.
The window says which addon it found and leaves it alone.

A row already bound to a whole multi-addon repository is flagged in the table as
*installs N addons*. The advice used to go into the Status column, which is 170 pixels
wide, so it was a smudge rather than a warning.

## Fixed in v0.5.1

**Set source did nothing in v0.5.0.** Choosing any source in the window — a repo, a
folder, or Unmanaged — failed silently: nothing was saved, and the table redrew the value
that was already there. It looked like the app reverting your choice on purpose. The
manifest was never touched, so nothing was lost and nothing needs undoing; re-set the
source on v0.5.1 and it holds. The terminal (`addons.py set`) was never affected.

An unexpected error in the window now says so, instead of going to a console a windowed
build does not have. That is what turned a one-line bug into something that looked like
the program disobeying you.

## New since v0.3.1

**Several WoW folders at once.** A vanilla server, a Wrath one and retail are separate
installs — separate AddOns directories, separate bindings, nothing shared. Run `init`
again to add one; a picker appears above the table once there is more than one. The same
addon can be bound differently in each, which is usually the reason for having two.

Your existing manifest becomes a single install named after its WoW folder, the first time
this reads it. Nothing you have bound is lost or needs redoing.

**One addon out of a repository that holds several.** Some people keep every addon they
have written in one repository. Binding an addon to such a repo used to install all of
them, and — because the repo has a single commit history — made every addon in it report
an update whenever any one of them changed. Naming the folder fixes both:

```
github:owner/repo#MyAddon
```

The plainest way to say it is to click into that addon on github.com and paste the address
you land on; the window reads the repository, the branch and the folder straight out of it.
Each addon in the repo then updates, and reports updates, on its own.

**A folder this tool did not install is never deleted, even in the same archive.** The
decision to keep a copy was taken once from the addon you bound and then applied to every
folder the archive landed. Updating one addon of nine could therefore delete the other
eight without keeping anything, on the strength of a record that described only the first.
It is now asked separately for every folder.

**Addons are found in more layouts.** `src/MyAddon/MyAddon.toc` beside a `docs/` folder
used to report "no addon folder found". An addon's own bundled libraries are still never
mistaken for the addon itself.

Also: an SSH clone URL (`git@github.com:owner/repo.git`) set as a source no longer splits
on its own `@` into a repository called `git`.

## Fixed in v0.3.1

**If you used v0.3.0, please read this one.** Updating an addon from a GitHub source
**deleted** the folder that was already there instead of keeping a copy of it — even
though the window said it would move it aside to `<Name>.replaced`. If you bound an addon
to a repository on v0.3.0 and had edited anything inside that addon's own folder, it is
gone. Saved variables normally live in `WTF/Account/…` rather than `Interface/AddOns/`, so
in most cases nothing of yours was in there — but it is worth a look. v0.3.0 has been
withdrawn for this reason.

Also in this release:

- **Copies are kept once, not on every update.** Only a folder this tool did not install
  is kept, so `<Name>.replaced2`, `.replaced3` cannot pile up. A checkbox in the Set-source
  dialog, or `--no-backup`, turns even the one copy off.
- **Paste a repository link.** The page URL, the clone URL, the SSH one, or a link to a
  branch — which is understood as that branch. `owner/repo` still works.
- **Check for updates**, with a **Latest** column beside Installed. It downloads nothing
  and writes nothing, so seeing what is out of date does not commit you to installing it.

## Download

| You are on | Get | Then |
|---|---|---|
| **Linux** | `WoW-Addons-from-GitHub-x86_64.AppImage` | `chmod +x` it and open it |
| **Windows** | `WoW-Addons-from-GitHub-windows-x64.zip` | unzip anywhere, run **WoW Addons from GitHub.exe** |

Both carry their own Python and their own Tk. Nothing to install, nothing to keep updated,
and deleting the file (or the folder) uninstalls it. `SHA256SUMS.txt` is attached if you
want to check what you downloaded.

The same download is also the command line — give it arguments and it behaves like the
script:

```
./WoW-Addons-from-GitHub-x86_64.AppImage update --check
"WoW Addons from GitHub.exe" update --check
```

## Two things that will look like faults and are not

- **Windows says "Windows protected your PC".** The `.exe` is not code-signed, so
  SmartScreen warns on first run. Click **More info → Run anyway**. A signing certificate
  is the only thing that removes it and costs a few hundred dollars a year.
- **Linux says `libfuse.so.2` is missing.** Recent Debian and Ubuntu stopped shipping
  FUSE 2. Either install it (`sudo apt install libfuse2`, or `libfuse2t64` on newer
  releases) or skip it entirely with `--appimage-extract-and-run`.

## Why this is marked a pre-release

Every build is checked by CI on both platforms: it starts, Tk really works inside the
bundle, the window opens and stays open, and it scans a folder end to end. **But nobody
has yet run either download on a desktop against a real WoW install.** That is a different
thing, and until somebody has, "pre-release" is the honest label.

If you try it, the two places to watch are the folder picker against a real
`Interface/AddOns`, and a `local:` source under Wine or Proton.

## What it does

- Binds each installed addon to where its updates come from: a GitHub repo, a branch, or a
  folder on your own disk.
- A `local:` source installs as a link, so `git pull` in that checkout *is* the update —
  a symlink on Linux, a directory junction on Windows (which needs no administrator
  rights).
- **It keeps the first copy of anything you installed yourself.** A folder this tool did
  not put there is moved to `<Name>.replaced`, beside the addon in your AddOns folder, and
  the window names it before you commit. After that the folder is one this tool wrote, so
  later updates replace it directly rather than making another copy.
- One unreachable repository does not sink the run — that addon is marked failed and
  everything else still updates.

Full documentation is in the [README](https://github.com/Digigull/WoW-Addons-from-Github#readme).
