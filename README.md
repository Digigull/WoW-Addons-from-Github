# WoW Addons from GitHub

Install and update World of Warcraft addons from whatever repositories **you** choose.

Point it at your WoW folder, let it scan what is already installed, bind each addon to
where its updates should come from, and run `update`.

```
python3 addons.py init ~/Games/Ascension
python3 addons.py scan
python3 addons.py set MyAddon github:someone/MyAddon
python3 addons.py update
```

## Why

The addon managers for private realms tend to have two problems: the catalogue is theirs, so
you cannot point an addon at an arbitrary repository, and some of them report what you
install whether or not you opted in.

This does neither. There is no catalogue, no account and **no telemetry of any kind** — it
contacts exactly the hosts named in your own manifest and nothing else. `update` reaches
`api.github.com` only for addons you have actually bound to a GitHub repository, and reaches
nothing at all for addons you keep on local disk.

It is a command-line tool, in one file, using nothing but the Python standard library. There
is nothing to install and nothing to keep updated.

## Requirements

Python 3.9 or newer. That is the whole list.

- **Linux:** already present on essentially every distribution.
- **Windows:** `winget install Python.Python.3.12`, or python.org. A packaged `.exe` that
  removes this step is planned — see [Windows](#windows) below.

## Commands

| Command | What it does |
|---|---|
| `init <path>` | Remember where the client is. Accepts your WoW folder, its `Interface` folder, or `Interface/AddOns` itself. |
| `scan` | Read every addon already installed and record it, guessing a source from each `.toc` where it can. |
| `list` | Every addon, its source, and its installed version. |
| `set <Addon> <source>` | Bind one addon to where its updates come from. |
| `accept` | Take every source that `scan` suggested, in one go. |
| `update [Addon...]` | Bring bound addons up to date. Defaults to all of them. |
| `where` | Print the manifest and AddOns paths. |

Useful flags on `update`: `--check` (report what is out of date, download nothing),
`--dry-run`, `--force` (reinstall even when the version matches).

## Sources

| Source | Behaviour |
|---|---|
| `local:<path>` | A folder on disk. Installed as a **symlink** by default, so `git pull` in that checkout *is* the update — nothing to reinstall, and the client cannot be running something other than what is checked out. `--copy` for real files instead. |
| `github:owner/repo` | Latest release, preferring an attached `.zip` and falling back to the source archive. |
| `github:owner/repo@branch` | That branch's current head, for an addon that does not cut releases. |
| `unmanaged` | Left alone. The default for anything `scan` finds and cannot place. |

`scan` reads each `.toc` for an `X-Website` or `X-Repository` header and *suggests* a GitHub
source where it finds one. Suggestions are never applied on their own — a header is the
author's claim about where the code lives, which is not the same as your decision to install
from there. `accept` applies them all once you have looked.

Archives are unpacked whatever shape they arrive in: a packaged release
(`MyAddon/MyAddon.toc`), GitHub's source archive (`repo-1a2b3c/MyAddon/MyAddon.toc`), and a
repository whose root *is* the addon (`repo-1a2b3c/MyAddon.toc`) all end up correctly as
`AddOns/MyAddon/MyAddon.toc`.

> Pointing a `github:` source at a repository containing **several** addon folders installs
> all of them. That is intended for multi-addon repositories, but it will surprise you if you
> expected one.

## Things it will not do to your client

- **It never deletes an addon you already had.** Binding an addon that exists as real files
  moves the old folder to `<Name>.replaced` first. Nothing inside it matches that name, so
  the client ignores it; delete it yourself once you are satisfied.
- **One failed source does not sink the run.** An unreachable, private or renamed repository
  is reported and skipped — everything else still updates, and the manifest still saves.
- **Archives containing `../` paths are refused.** This unpacks zips published by third
  parties.
- **Your manifest stays out of the way**, at `$XDG_CONFIG_HOME/wow-addons/manifest.json` —
  in practice `~/.config/wow-addons/manifest.json`, and `C:\Users\<you>\.config\wow-addons\`
  on Windows. It holds your disk paths, so it does not belong in a repository. `where` prints
  the resolved location.

`GITHUB_TOKEN` is honoured if set, and is entirely optional: unauthenticated GitHub allows 60
requests an hour, which is far more than a personal addon list needs.

Restart the client, or `/reload`, to pick changes up.

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

The tool runs on Windows today with Python installed, and `github:` sources work normally.

Two things are still Linux-shaped, and both are small:

1. **`local:` sources create a real symlink**, which on Windows needs administrator rights or
   Developer Mode. Directory *junctions* need neither and are the correct mechanism there.
   Until that lands, use `--copy` for `local:` sources on Windows.
2. **The manifest goes to `~/.config/wow-addons/`** rather than `%APPDATA%`, which works but
   is not where a Windows tool should keep it.

A packaged `.exe` is planned after those, so Windows users need no Python install at all.

## Development

```
python3 -m unittest discover -s tests -t . -v
```

17 assertions, no network and no game client — archives are built in memory and the GitHub
API is stubbed. They run in well under a second, and CI runs them on Linux and Windows
against Python 3.9 and 3.12.

The suite is not there for coverage. It pins the things that actually broke, plus the guards
whose failure would otherwise be silent: an archive whose root is the addon installing under
GitHub's wrapper name (which the client ignores), and a `403` reported as a rate limit when
the real cause was a blocked proxy or a private repository.

## Licence

MIT — see [LICENSE](LICENSE).
