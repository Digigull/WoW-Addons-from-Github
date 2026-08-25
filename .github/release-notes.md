Update your World of Warcraft addons from repositories **you** choose. No catalogue, no
account, and no telemetry of any kind — it contacts exactly the hosts named in your own
manifest and nothing else.

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
- **It never deletes an addon you already had.** Anything it would replace is moved to
  `<Name>.replaced` first, and the window tells you the name before you commit to it.
- One unreachable repository does not sink the run — that addon is marked failed and
  everything else still updates.

Full documentation is in the [README](https://github.com/Digigull/WoW-Addons-from-Github#readme).
