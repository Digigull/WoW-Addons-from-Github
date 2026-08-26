Update your World of Warcraft addons from repositories **you** choose. No catalogue, no
account, and no telemetry of any kind — it contacts exactly the hosts named in your own
manifest and nothing else.

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
