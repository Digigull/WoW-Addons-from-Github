"""The window. One window is enough: a table, four buttons and one dialog.

Tkinter, from the standard library, because the project's defining property is
that there is nothing to install -- and that has to stay true all the way to
the UI or it was never true at all. `ttk` widgets throughout, not the ancient
`tk.*` ones: same standard library, considerably less dated.

THREADING

Every network call is off the UI thread, because a window that freezes
mid-download reads on Windows as "not responding" and gets force-quit halfway
through an install. The standard Tk pattern, no new dependencies:

    the worker pushes (kind, payload) onto a queue.Queue
    the UI drains it from a root.after(POLL_MS) poll
    only the drain touches widgets

Tk is not thread-safe and violations show up as intermittent crashes rather
than as anything that looks like a threading bug, so the rule is absolute:
`_Worker` may not touch a widget, and does not import one.

Cancellation is a threading.Event checked between addons. Mid-download
cancellation is not worth the complexity -- stopping between addons is enough,
and it leaves the manifest consistent.
"""

from __future__ import annotations

import queue
import threading
import traceback
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, core
from .core import Fail

ISSUES_URL = "https://github.com/Digigull/WoW-Addons-from-Github/issues"

POLL_MS = 100
UNMANAGED_LABEL = "(unmanaged)"


# ── the worker ───────────────────────────────────────────────────────────────


class _Worker(threading.Thread):
    """Runs updates off the UI thread and reports back through a queue.

    Deliberately knows nothing about Tk. It takes the entries it was given,
    calls core.update_addon on each, and posts the results; the window decides
    what any of that looks like.
    """

    def __init__(self, names, entries, root, outbox, *, force=False, check=False, no_api=False):
        super().__init__(daemon=True)
        self.no_api = no_api
        self.names = list(names)
        self.entries = entries
        self.root = root
        self.outbox = outbox
        self.force = force
        self.check = check
        self.cancelled = threading.Event()

    def run(self) -> None:
        total = len(self.names)
        try:
            for index, name in enumerate(self.names):
                if self.cancelled.is_set():
                    self.outbox.put(("cancelled", index))
                    return
                self.outbox.put(("begin", (name, index, total)))
                entry = self.entries.get(name)
                if entry is None:
                    self.outbox.put(("result", core.Result(name, core.FAILED, "not in the manifest")))
                    continue

                def progress(stage, detail, _name=name):
                    self.outbox.put(("progress", (_name, stage, detail)))

                # The engine paces its GitHub calls, and a pause it does not
                # announce is indistinguishable from a window that has frozen.
                core.set_wait_hook(
                    lambda seconds, why, _name=name: self.outbox.put(("waiting", (_name, seconds, why)))
                )
                result = core.update_addon(
                    name, entry, self.root, force=self.force, check=self.check,
                    no_api=self.no_api, progress=progress,
                )
                self.outbox.put(("result", result))
        finally:
            core.set_wait_hook(None)
            self.outbox.put(("done", None))


# ── the half both repository dialogs share ───────────────────────────────────


def repo_named(spec: str, message: str) -> bool:
    """Does this message already say which repository it is about?"""
    return spec in message or spec.split("@")[0] in message


class RepoDialog:
    """What Set source and Install have in common: a repository box, and the
    question "what addons does this repository hold?"

    Both ask GitHub that, off the UI thread, a moment after typing stops, and
    draw the same tick boxes from the answer. One implementation, because two
    would drift -- and the drift would be in which folders a person is offered,
    which is the one thing either dialog is for.

    A host provides the widgets (`repo_hint`, `lookup_status`, `addon_boxes`,
    `addon_list`) and the tk variables named in its own VARIABLES, says what
    starts ticked (`_preticked`) and what a tick means to it
    (`_folders_ticked`).
    """

    def _init_lookup(self, *, no_api: bool) -> None:
        # Defaults to False so a test, or any other caller, gets the ordinary
        # API lookup without having to know this mode exists.
        self.checks_without_api = no_api
        self.folder_boxes: dict[str, tk.BooleanVar] = {}
        # What the repository holds, and -- separately -- what of it is being
        # offered as a choice. They differ for a repo holding one addon: there
        # is nothing to choose, and its folder name is still worth knowing.
        self.available: list[str] = []
        self.looked_up: list[str] = []
        self.lookup_for = ""
        self.lookups: queue.Queue = queue.Queue()
        self._lookup_after = None
        self._poll_after = None

    def _preticked(self, folders: list[str]) -> set[str]:
        """Which boxes start ticked. Nothing, unless the host knows better."""
        return set()

    def _client_choice(self, folders: list[str]) -> bool:
        """Is the choice on offer which .toc, rather than which addon?

        A repository that IS the addon and ships one .toc per client offers
        names, not folders -- and the choice between them is which game you
        play, which is not a thing this tool can work out.
        """
        return bool(folders) and all(core.names_a_toc(folder) for folder in folders)

    def _pick_message(self, folders: list[str]) -> str:
        if self._client_choice(folders):
            return (f"one addon, {len(folders)} .toc files — one per client. "
                    "Tick the one yours uses")
        return f"{len(folders)} addons — tick the ones this row updates"

    def _folders_ticked(self, *_a) -> None:
        """What a tick means to this dialog."""

    def destroy(self) -> None:
        """Close, and let go of the tk variables while it is still safe to.

        A tkinter Variable calls into the interpreter when it is garbage
        collected. Left to the collector that happens at an arbitrary moment on
        whatever thread happened to be allocating -- and this program has a
        worker thread. Collected there, Tcl raises "main thread is not in main
        loop", and on Windows it escalates to aborting the whole process with
        "Tcl_AsyncDelete: async handler deleted by the wrong thread". A user
        would see that as the app vanishing part-way through an update, having
        closed this dialog some time earlier.

        Releasing them here pins that moment to the main thread, during close,
        which is the only point at which it is certainly safe.
        """
        self._stop_lookups()

        held = [getattr(self, name, None) for name in self.VARIABLES]
        # The tick boxes are variables too, made after __init__ so the
        # VARIABLES list does not cover them. Clearing the dict is what
        # actually releases them; extending `held` first only makes them
        # finalise at the same moment as the rest, after super().destroy(),
        # rather than a few lines earlier. Same thread either way, which is
        # the part that matters.
        held.extend(self.folder_boxes.values())
        self.folder_boxes.clear()
        for name in self.VARIABLES:
            setattr(self, name, None)
        super().destroy()
        held.clear()  # __del__ runs now, on this thread, with Tcl still up

    def _absorb_url(self, *_a) -> None:
        """Show what a pasted URL was understood as, while it is being pasted.

        Silently accepting a URL and only revealing the interpretation after
        the button is pressed leaves somebody guessing whether it took the
        branch they meant.
        """
        if self.repo is None:
            return
        text = self.repo.get()
        found = core.parse_repo(text)
        if found is None:
            self.repo_hint.configure(text="not a GitHub repository" if text.strip() else "")
            return
        repo, branch, folder = found
        if branch and not self.track.get():
            # The URL named a branch; reflect that rather than dropping it.
            self.track.set(True)
            self.branch.set(branch)
            self._sync()
        # Clicking into one addon of several and copying the address is the
        # clearest way anybody states which addon they mean. Keep it.
        self._absorbed(folder)
        shown = f"→ {repo}"
        if branch:
            shown += f" @ {branch}"
        if folder:
            shown += f" · {folder}"
        self.repo_hint.configure(text=shown)
        self._schedule_lookup()

    def _absorbed(self, folder: str | None) -> None:
        """Keep the folder a pasted URL named, if this dialog has a use for it."""

    def _centre(self, parent) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _stop_lookups(self) -> None:
        """Timers first: an after() that fires into a half-torn-down dialog
        reaches for widgets that are gone."""
        for pending in ("_lookup_after", "_poll_after"):
            token = getattr(self, pending, None)
            if token is not None:
                self.after_cancel(token)
                setattr(self, pending, None)


    def _schedule_lookup(self) -> None:
        """Ask GitHub a moment after typing stops, not on every keystroke.

        Typing `tullamods/Bagnon` would otherwise be sixteen requests for one
        answer. The delay is cancelled and re-armed on each key, so exactly one
        goes out per repository somebody actually settles on.
        """
        if self._lookup_after is not None:
            self.after_cancel(self._lookup_after)
        self._lookup_after = self.after(600, self._begin_lookup)

    def _begin_lookup(self) -> None:
        self._lookup_after = None
        if self.repo is None:
            return
        found = core.parse_repo(self.repo.get())
        if found is None:
            self._hide_list()
            return
        repo, branch, _folder = found
        spec = f"{repo}@{branch}" if branch else repo
        if spec == self.lookup_for:
            return
        self.lookup_for = spec
        self._show_list(f"looking in {repo}…", [])

        # A worker, because a request on the main thread freezes the window --
        # and nothing here touches a widget: it puts a result in a queue and
        # _drain_lookups picks it up on the main thread. That rule is why this
        # program does not abort with Tcl_AsyncDelete.
        no_api = self.checks_without_api
        def ask() -> None:
            core.begin_run()
            try:
                self.lookups.put((spec, core.addons_in_repo(spec, no_api=no_api), None))
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                self.lookups.put((spec, [], str(exc)))
            finally:
                core.end_run()

        threading.Thread(target=ask, daemon=True).start()
        self._poll_lookups()

    def _poll_lookups(self) -> None:
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
        self._poll_after = self.after(100, self._drain_lookups)

    def _drain_lookups(self) -> None:
        self._poll_after = None
        if self.repo is None:
            return  # closed while the request was in flight
        pending = False
        while True:
            try:
                spec, folders, error = self.lookups.get_nowait()
            except queue.Empty:
                break
            if spec != self.lookup_for:
                continue  # an answer about a repo that has since been retyped
            pending = True
            self.available = list(folders)
            if error:
                # The engine's message usually names the repository itself --
                # "cannot see o/r. Either it does not exist, or ...". Prefixing
                # that with "could not read o/r:" says the name twice and
                # pushes the half that tells you what to do off the end.
                self._show_list(error if repo_named(spec, error)
                                else f"could not read {spec}: {error}", [])
            elif not folders:
                # The repository root is the addon -- FrostSeek, Minn-Tinkers.
                # There is nothing to choose, so say so and offer no choice.
                self._show_list("one addon, installed whole — nothing to choose", [])
            elif len(folders) == 1:
                # One candidate is not a choice. Offering a single tick box
                # would imply a decision, and ticking it would do real harm:
                # naming a folder switches this row from the repository's
                # RELEASES to the last commit touching that folder, so an addon
                # that publishes tagged releases would silently start reporting
                # commit ids instead of version numbers. Left unticked it
                # installs exactly the same folder, and keeps its releases.
                self._show_list(f"one addon: {folders[0]} — nothing to choose", [])
            else:
                self._show_list(self._pick_message(folders), folders)
        if not pending:
            self._poll_lookups()

    def _hide_list(self) -> None:
        self.lookup_for = ""
        self.available = []
        self.looked_up = []
        self.addon_list.grid_remove()

    def _show_list(self, message: str, folders: list[str]) -> None:
        self.looked_up = folders
        self.lookup_status.configure(text=message)
        for child in self.addon_boxes.winfo_children():
            child.destroy()
        self.folder_boxes.clear()

        ticked = self._preticked(folders)
        for row, folder in enumerate(folders):
            variable = tk.BooleanVar(value=folder in ticked)
            self.folder_boxes[folder] = variable
            ttk.Checkbutton(
                self.addon_boxes, text=folder, variable=variable,
                command=self._folders_ticked,
            ).grid(row=row, column=0, sticky="w")
        if folders:
            self._folders_ticked()
        self.addon_list.grid()


# ── the set-source dialog ────────────────────────────────────────────────────


class SourceDialog(RepoDialog, tk.Toplevel):
    """Where one addon's source is chosen. Returns (source, copy) or None.

    The displacement warning lives here rather than after Save on purpose: this
    is the confirm step, and moving a real folder aside is the one thing this
    tool does that cannot be undone. In a terminal you read about it in the log
    afterwards; in a window there is no log, so it has to be said in advance.
    """

    # Every tk variable this dialog owns. Named once, so destroy() cannot drift
    # out of step with __init__.
    VARIABLES = ("choice", "local", "repo", "branch", "track", "copy", "backup", "folder")

    def __init__(self, parent, addon: str, entry: dict, root: Path, *, no_api: bool = False):
        super().__init__(parent)
        self.title(f'Source for "{addon}"')
        self.addon = addon
        self.addons_root = root
        self._init_lookup(no_api=no_api)
        self.entry = entry
        self.result: tuple[str, bool] | None = None
        self.keep_backup = entry.get("backup", True)
        self.transient(parent)
        self.resizable(False, False)

        source = entry.get("source", "unmanaged")
        suggested = entry.get("suggested")

        self.choice = tk.StringVar(value="unmanaged")
        self.local = tk.StringVar()
        self.repo = tk.StringVar()
        self.branch = tk.StringVar()
        self.track = tk.BooleanVar(value=False)
        self.copy = tk.BooleanVar(value=entry.get("mode") == "copy")
        self.backup = tk.BooleanVar(value=entry.get("backup", True))
        # Ticked boxes write into `self.folder`, which stays the single source
        # of truth: _save reads only that, so a typed folder and a ticked one
        # cannot disagree, and a repository too large to list is still usable.
        self.folder = tk.StringVar()

        if source.startswith("local:"):
            self.choice.set("local")
            self.local.set(source[len("local:"):])
        elif source.startswith("github:"):
            self.choice.set("github")
            repo, branch, folder = core.split_repo_spec(source[len("github:"):])
            self.repo.set(repo)
            if branch:
                self.branch.set(branch)
                self.track.set(True)
            if folder:
                self.folder.set(folder)
        elif suggested and suggested.startswith("github:"):
            # A suggestion is offered pre-filled but never pre-selected: the
            # .toc header is the author's claim about where the code lives, not
            # this user's decision to install from there.
            self.repo.set(suggested[len("github:"):])

        self._build(suggested)
        self._sync()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._save())
        self._centre(parent)
        # Order matters: a grab on a window that is not on screen yet fails, so
        # wait for it to map first.
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _build(self, suggested: str | None) -> None:
        pad = {"padx": 8, "pady": 3}
        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        ttk.Radiobutton(body, text="Local folder", value="local", variable=self.choice,
                        command=self._sync).grid(row=0, column=0, sticky="w", **pad)
        self.local_entry = ttk.Entry(body, textvariable=self.local, width=44)
        self.local_entry.grid(row=0, column=1, sticky="ew", **pad)
        # Re-check as the path is edited: which folder is at risk depends on
        # what is typed, so a warning that only updated on a radio click would
        # be stale the moment someone pointed at a different checkout.
        #
        # A widget binding rather than a StringVar trace, deliberately: a trace
        # outlives the dialog and Tcl tears it down from whatever thread the
        # garbage collector happened to be on, which aborts the process with
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread".
        self.local_entry.bind("<KeyRelease>", self._show_caution)
        self.browse = ttk.Button(body, text="Browse…", command=self._browse)
        self.browse.grid(row=0, column=2, **pad)
        self.copy_box = ttk.Checkbutton(body, text="copy files instead of linking", variable=self.copy)
        self.copy_box.grid(row=1, column=1, sticky="w", **pad)

        ttk.Radiobutton(body, text="GitHub repo", value="github", variable=self.choice,
                        command=self._sync).grid(row=2, column=0, sticky="w", **pad)
        self.repo_entry = ttk.Entry(body, textvariable=self.repo, width=44)
        self.repo_entry.grid(row=2, column=1, sticky="ew", **pad)
        self.repo_entry.bind("<KeyRelease>", self._absorb_url)
        ttk.Label(body, text="or a github.com link", foreground="grey").grid(
            row=2, column=2, sticky="w", **pad)

        self.repo_hint = ttk.Label(body, text="", foreground="grey")
        self.repo_hint.grid(row=3, column=2, sticky="w", **pad)

        track = ttk.Frame(body)
        track.grid(row=3, column=1, sticky="w", **pad)
        self.track_box = ttk.Checkbutton(track, text="track branch:", variable=self.track, command=self._sync)
        self.track_box.grid(row=0, column=0, sticky="w")
        self.branch_entry = ttk.Entry(track, textvariable=self.branch, width=18)
        self.branch_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))

        # For a repository holding several addons. Blank means "whatever the
        # archive contains", which is right for the ordinary one-addon repo and
        # is what every existing source keeps meaning.
        folder_row = ttk.Frame(body)
        folder_row.grid(row=9, column=1, sticky="ew", **pad)
        ttk.Label(folder_row, text="folder in repo:").grid(row=0, column=0, sticky="w")
        self.folder_entry = ttk.Entry(folder_row, textvariable=self.folder, width=26)
        self.folder_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.folder_entry.bind("<KeyRelease>", self._show_caution)
        ttk.Label(folder_row, text="(only if the repo holds several addons)",
                  foreground="grey").grid(row=0, column=2, sticky="w", padx=(6, 0))

        # What the repository actually contains, once it has been asked. Empty
        # until then, and hidden entirely for a repo holding a single addon --
        # there is nothing to choose and a one-item list would only imply there
        # were a decision to make.
        self.addon_list = ttk.LabelFrame(body, text="Addons in this repository")
        self.addon_list.grid(row=10, column=0, columnspan=3, sticky="ew", **pad)
        self.addon_list.grid_remove()
        self.lookup_status = ttk.Label(self.addon_list, text="", foreground="grey",
                                       wraplength=620, justify="left")
        self.lookup_status.grid(row=0, column=0, sticky="w", padx=6, pady=(2, 4))
        self.addon_boxes = ttk.Frame(self.addon_list)
        self.addon_boxes.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

        ttk.Radiobutton(body, text="Leave unmanaged", value="unmanaged", variable=self.choice,
                        command=self._sync).grid(row=4, column=0, sticky="w", **pad)

        # Applies to both source types, so it sits outside the radio group.
        self.backup_box = ttk.Checkbutton(
            body,
            text="keep a copy of files I installed myself, the first time they are replaced",
            variable=self.backup,
            command=self._show_caution,
        )
        self.backup_box.grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        if suggested:
            ttk.Label(body, text=f"This addon's .toc suggests {suggested}", foreground="grey").grid(
                row=6, column=0, columnspan=3, sticky="w", **pad)

        if self.choice.get() == "github" and self.repo.get().strip():
            # Already bound: show what the repository holds without waiting for
            # a keystroke, so the ticks can be read against what is saved.
            self.after(50, self._begin_lookup)

        self.caution = ttk.Label(body, text="", foreground="#a05000", wraplength=480, justify="left")
        self.caution.grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=1, padx=4)
        body.columnconfigure(1, weight=1)

    def _absorbed(self, folder: str | None) -> None:
        if folder and self.folder is not None and not self.folder.get().strip():
            self.folder.set(folder)

    def _preticked(self, folders: list[str]) -> set[str]:
        """What is saved, else one confident guess -- and no guess at all rather
        than a wrong one, which would arrive ticked and be accepted unread."""
        already = core.wanted_folders(self.folder.get())
        if already:
            return set(already)
        guess = core.likely_addon(self.addon, folders)
        return {guess} if guess else set()

    def _folders_ticked(self, *_a) -> None:
        """Ticked boxes are written into the folder box, which _save reads."""
        if self.folder is None:
            return
        self.folder.set(",".join(f for f, v in self.folder_boxes.items() if v.get()))
        self._show_caution()

    def _sync(self, *_a) -> None:
        """Grey out whatever the current choice does not use."""
        if self.choice is None:
            return  # a queued event arriving after the dialog was closed
        choice = self.choice.get()
        local = "normal" if choice == "local" else "disabled"
        github = "normal" if choice == "github" else "disabled"
        self.local_entry.configure(state=local)
        self.browse.configure(state=local)
        self.copy_box.configure(state=local)
        self.repo_entry.configure(state=github)
        self.track_box.configure(state=github)
        self.branch_entry.configure(state="normal" if choice == "github" and self.track.get() else "disabled")
        self.folder_entry.configure(state=github)
        self._show_caution()

    def _provisional(self) -> dict:
        """The entry this dialog would save, for asking core what it would do."""
        choice = self.choice.get() if self.choice is not None else "unmanaged"
        if choice == "local":
            path = self.local.get().strip()
            source = f"local:{path}" if path else "unmanaged"
        elif choice == "github":
            # Only the scheme matters, except for the folder: for a repo of
            # several addons the folder is what lands in AddOns, and naming the
            # addon instead would caution about the wrong directory.
            folder = self.folder.get().strip("/ ") if self.folder is not None else ""
            source = f"github:owner/repo#{folder}" if folder else "github:owner/repo"
        else:
            source = "unmanaged"
        return {
            "source": source,
            "backup": bool(self.backup.get()) if self.backup is not None else True,
            # Both carried through: together they decide whether a folder is
            # this tool's to replace or the user's to keep.
            "installed": self.entry.get("installed"),
            "folders": self.entry.get("folders", []),
        }

    def _show_caution(self, *_a) -> None:
        """Say what will happen to files that are already there -- accurately.

        Accurately is the whole point. This used to promise a backup for every
        source type while archive installs deleted outright, and somebody lost
        a hand-installed addon to the difference. It now asks core the same
        questions core will ask itself.
        """
        if self.choice is None:
            return
        entry = self._provisional()
        doomed = core.displaced_folder(entry, self.addon, self.addons_root)
        if doomed is None:
            self.caution.configure(text="")
            return

        if entry.get("installed") and doomed.name in (entry.get("folders") or []):
            # A folder this tool wrote itself: replacing it loses nothing the
            # source cannot fetch again. Warning here would put a red line on
            # every routine update, which is how people learn to ignore the
            # warning that matters.
            #
            # The folder has to be checked, not just the version. Asking only
            # "has this addon been installed?" goes quiet for a mono-repo whose
            # bound folder is a directory this tool has never touched -- silence
            # about the wrong folder, which is how the last one went wrong.
            self.caution.configure(text="")
            return

        if core.should_backup_folder(entry, doomed.name):
            kept = core.backup_name(doomed)
            self.caution.configure(
                foreground="#a05000",
                text=f"⚠  {doomed.name} is real files in your AddOns folder right now, and "
                     f"this tool did not put them there. They will be moved aside to "
                     f"{kept.name} — once. Later updates replace it without another copy.",
            )
        else:
            self.caution.configure(
                foreground="#b00020",
                text=f"⚠  {doomed.name} is real files this tool did not install, and the "
                     f"backup is switched off — they will be DELETED, not kept.",
            )

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title=f"Folder holding {self.addon}", parent=self)
        if chosen:
            self.local.set(chosen)
            self._show_caution()

    def _save(self) -> None:
        choice = self.choice.get()
        self.keep_backup = bool(self.backup.get())
        if choice == "unmanaged":
            self.result = ("unmanaged", False)
        elif choice == "local":
            path = self.local.get().strip()
            if not path:
                messagebox.showerror("No folder", "Pick the folder the addon lives in.", parent=self)
                return
            self.result = (f"local:{path}", self.copy.get())
        else:
            # Whatever they pasted: owner/repo, the page URL, the clone URL, the
            # SSH one, or a link to a branch. Telling somebody who just pasted a
            # working URL to retype it by hand is a small insult.
            found = core.parse_repo(self.repo.get())
            if found is None:
                account = core.github_account(self.repo.get())
                if account:
                    # An organisation page names no repository, and is an easy
                    # thing to paste when the addons you want are published by
                    # one. "Not a GitHub repository" would read as though the
                    # link were broken.
                    messagebox.showerror(
                        "That is an account, not an addon",
                        f"{account} is a GitHub account, which may hold many addons.\n\n"
                        "Open the addon you want on github.com and paste that address —\n"
                        f"or write it as {account}/repo-name.",
                        parent=self,
                    )
                    return
                messagebox.showerror(
                    "Not a GitHub repository",
                    "Paste a github.com link, or write it as owner/repo.\n\n"
                    "Both of these work:\n"
                    "    tullamods/Bagnon\n"
                    "    https://github.com/tullamods/Bagnon",
                    parent=self,
                )
                return
            repo, url_branch, url_folder = found
            typed = self.branch.get().strip()
            # A branch in the pasted URL counts as asking to track it.
            branch = typed if (self.track.get() and typed) else (url_branch or "")
            folder = (self.folder.get().strip() or url_folder or "").strip("/")
            if not folder and self._client_choice(self.looked_up):
                # Not the same question as below. Every .toc here names the
                # same files, so "all of them" would install this one addon
                # two or three times over under names only one of which the
                # client will load. There is nothing to say OK to.
                messagebox.showerror(
                    "Which client?",
                    f"{repo} is one addon with {len(self.looked_up)} .toc files — one per "
                    "client version.\n\nThe folder in AddOns has to be named after the one "
                    "you want, so there is no sensible thing to install until you say "
                    "which.\n\nTick the .toc your client uses.",
                    parent=self,
                )
                return
            if not folder and len(self.looked_up) > 1:
                # Saving with nothing ticked is a real choice -- it binds the
                # whole repository -- but it is far more often an oversight, and
                # the consequence (every addon in the repo installed into your
                # AddOns folder) is not obvious from the dialog.
                if not messagebox.askokcancel(
                    "No addon ticked",
                    f"You have not ticked any of the {len(self.looked_up)} addons in "
                    f"{repo}.\n\nOK installs ALL of them whenever this row updates.\n"
                    "Cancel goes back so you can tick the one you want.",
                    parent=self, default=messagebox.CANCEL, icon=messagebox.WARNING,
                ):
                    return
            source = f"github:{repo}"
            if branch:
                source += f"@{branch}"
            if folder:
                source += f"#{folder}"
            self.result = (source, False)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()



# ── the install dialog ───────────────────────────────────────────────────────


class InstallDialog(RepoDialog, tk.Toplevel):
    """Where an addon you do not have yet comes from. Returns [(name, source)].

    Every other button in this window works on what is already in AddOns: a
    rescan finds a folder, Set source binds it to where its updates come from.
    Getting a NEW addon in meant downloading and unzipping it by hand first and
    then telling this tool about it -- and downloading and unzipping an addon
    correctly is the exact job this tool exists to do for you.

    It creates one row per addon and then installs it, rather than binding a
    row and leaving the install to be discovered: pressing Install and getting
    a row that says "not installed" would be a button that did not do what it
    said.
    """

    VARIABLES = ("repo", "branch", "track")

    def __init__(self, parent, root: Path, entries: dict, *, no_api: bool = False):
        super().__init__(parent)
        self.title("Install an addon")
        self.addons_root = root
        self.known = entries
        self.result: list[tuple[str, str]] | None = None
        self._init_lookup(no_api=no_api)
        self.transient(parent)
        self.resizable(False, False)

        self.repo = tk.StringVar()
        self.branch = tk.StringVar()
        self.track = tk.BooleanVar(value=False)

        self._build()
        self._sync()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._install())
        self._centre(parent)
        # Order matters: a grab on a window that is not on screen yet fails, so
        # wait for it to map first.
        self.wait_visibility()
        self.grab_set()
        self.repo_entry.focus_set()

    def _pick_message(self, folders: list[str]) -> str:
        if self._client_choice(folders):
            return (f"one addon, {len(folders)} .toc files — one per client. "
                    "Tick the one yours uses")
        return f"{len(folders)} addons — tick the ones to install"

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 3}
        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        ttk.Label(body, text="Install from:").grid(row=0, column=0, sticky="w", **pad)
        self.repo_entry = ttk.Entry(body, textvariable=self.repo, width=46)
        self.repo_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.repo_entry.bind("<KeyRelease>", self._absorb_url)
        self.repo_hint = ttk.Label(body, text="", foreground="grey")
        self.repo_hint.grid(row=0, column=2, sticky="w", **pad)
        ttk.Label(body, text="owner/repo, or a github.com link", foreground="grey").grid(
            row=1, column=1, sticky="w", **pad)

        track = ttk.Frame(body)
        track.grid(row=2, column=1, sticky="w", **pad)
        self.track_box = ttk.Checkbutton(track, text="track branch:", variable=self.track,
                                         command=self._sync)
        self.track_box.grid(row=0, column=0, sticky="w")
        self.branch_entry = ttk.Entry(track, textvariable=self.branch, width=18)
        self.branch_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(track, text="(otherwise: its latest release)", foreground="grey").grid(
            row=0, column=2, sticky="w", padx=(6, 0))

        # Hidden until a repository has been read, and hidden again for one
        # holding a single addon: there is nothing to choose, and a one-item
        # list would imply there were a decision to make.
        self.addon_list = ttk.LabelFrame(body, text="Addons in this repository")
        self.addon_list.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.addon_list.grid_remove()
        self.lookup_status = ttk.Label(self.addon_list, text="", foreground="grey",
                                       wraplength=620, justify="left")
        self.lookup_status.grid(row=0, column=0, sticky="w", padx=6, pady=(2, 4))
        self.addon_boxes = ttk.Frame(self.addon_list)
        self.addon_boxes.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

        self.caution = ttk.Label(body, text="", foreground="#a05000", wraplength=480,
                                 justify="left")
        self.caution.grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Install", command=self._install).grid(row=0, column=1, padx=4)
        body.columnconfigure(1, weight=1)

    def _folders_ticked(self, *_a) -> None:
        self._sync()

    def _show_list(self, message: str, folders: list[str]) -> None:
        # What the repository turns out to hold changes what Install would do,
        # so the caution has to be re-read against the answer -- including the
        # answer "one addon", which offers no tick box to trigger it.
        super()._show_list(message, folders)
        self._sync()

    def _sync(self, *_a) -> None:
        if self.repo is None:
            return  # a queued event arriving after the dialog was closed
        self.branch_entry.configure(state="normal" if self.track.get() else "disabled")
        self._show_caution()

    # -- what Install would do -----------------------------------------------

    def _spec(self) -> str | None:
        """The repository this dialog is pointed at, or None if it is not one yet."""
        found = core.parse_repo(self.repo.get()) if self.repo is not None else None
        if found is None:
            return None
        repo, url_branch, url_folder = found
        typed = self.branch.get().strip()
        # A branch in the pasted URL counts as asking to track it.
        branch = typed if (self.track.get() and typed) else (url_branch or "")
        spec = repo + (f"@{branch}" if branch else "")
        return spec + (f"#{url_folder}" if url_folder else "")

    def _ticked(self) -> list[str]:
        return [folder for folder, box in self.folder_boxes.items() if box.get()]

    def _plan(self) -> list[tuple[str, str]]:
        """The rows Install would create. Empty while there is nothing to act on."""
        spec = self._spec()
        if spec is None:
            return []
        return core.install_plan(spec, self._ticked(), self.available)

    def _show_caution(self, *_a) -> None:
        """Say what is about to happen to folders that are already there.

        The same rule as Set source, for the same reason: replacing files
        somebody installed by hand is the one thing here that cannot be undone,
        and in a window there is no log to read about it afterwards.
        """
        if self.repo is None:
            return
        if len(self.looked_up) > 1 and not self._ticked():
            self.caution.configure(
                foreground="grey",
                text="Tick the .toc your client uses." if self._client_choice(self.looked_up)
                     else "Tick the addons you want.",
            )
            return

        lines = []
        for name, source in self._plan():
            entry = dict(self.known.get(name) or core.new_entry(name))
            bound = entry.get("source", "unmanaged")
            if bound != "unmanaged" and bound != source:
                lines.append(f"{name} is already bound to {core.tilde(bound)}; installing "
                             f"re-binds it to this repository.")
            entry["source"] = source
            doomed = core.displaced_folder(entry, name, self.addons_root)
            if doomed is None or (entry.get("installed") and doomed.name in (entry.get("folders") or [])):
                continue
            if core.should_backup_folder(entry, doomed.name):
                lines.append(f"⚠  {doomed.name} is real files in your AddOns folder right now, "
                             f"and this tool did not put them there. They will be moved aside "
                             f"to {core.backup_name(doomed).name} — once.")
            else:
                lines.append(f"⚠  {doomed.name} is real files this tool did not install, and "
                             f"the backup is switched off — they will be DELETED, not kept.")
        self.caution.configure(
            foreground="#a05000" if any(line.startswith("⚠") for line in lines) else "#666666",
            text="\n".join(lines),
        )

    # -- the buttons ---------------------------------------------------------

    def _install(self) -> None:
        if self._spec() is None:
            account = core.github_account(self.repo.get())
            if account:
                # An organisation page names no repository, and is an easy thing
                # to paste when the addons you want are published by one.
                messagebox.showerror(
                    "That is an account, not an addon",
                    f"{account} is a GitHub account, which may hold many addons.\n\n"
                    "Open the addon you want on github.com and paste that address —\n"
                    f"or write it as {account}/repo-name.",
                    parent=self,
                )
                return
            messagebox.showerror(
                "Nothing to install",
                "Paste a github.com link, or write it as owner/repo.\n\n"
                "Both of these work:\n"
                "    tullamods/Bagnon\n"
                "    https://github.com/tullamods/Bagnon",
                parent=self,
            )
            return

        plan = self._plan()
        if not plan:
            # The only way to get here: a repository of several addons with
            # nothing ticked. Installing all of them is a choice Set source
            # offers deliberately; it is not what an Install button should do
            # by default, and here it can simply be asked for instead.
            if self._client_choice(self.looked_up):
                messagebox.showerror(
                    "Which client?",
                    f"{self._spec()} is one addon with {len(self.looked_up)} .toc files — "
                    "one per client version.\n\nThe folder in AddOns has to be named after "
                    "the one you want, so there is no sensible thing to install until you "
                    "say which.\n\nTick the .toc your client uses.",
                    parent=self,
                )
                return
            messagebox.showerror(
                "Which addon?",
                f"{self._spec()} holds {len(self.looked_up)} addons.\n\n"
                "Tick the ones you want to install.",
                parent=self,
            )
            return

        rebinding = [name for name, source in plan
                     if self.known.get(name, {}).get("source", "unmanaged")
                     not in ("unmanaged", source)]
        if rebinding and not messagebox.askokcancel(
            "Already bound",
            f"{', '.join(rebinding)} already has a source set.\n\n"
            "OK re-binds it to this repository and installs from there.\n"
            "Cancel leaves it as it is.",
            parent=self, default=messagebox.CANCEL, icon=messagebox.WARNING,
        ):
            return

        self.result = plan
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# ── the overwrite confirmation ───────────────────────────────────────────────


class SignInDialog(tk.Toplevel):
    """Where a GitHub token gets in, so that a private repository is visible.

    Without a token GitHub answers 404 for a private repository -- the same
    answer it gives for one that does not exist, because saying anything else
    would leak which private repositories exist. So "could not read" was the
    whole of the story, from a window with nowhere to put the thing that fixes
    it. This is that place.

    A fine-grained token rather than a classic one, and the wording says so
    twice, because the difference is the entire security argument: a classic
    token with `repo` can read and WRITE every private repository the person
    can reach, while a fine-grained one can be limited to the single addon
    repository, read-only, with an expiry date. Both work here. Only one of
    them is a reasonable thing to leave saved on a gaming machine.
    """

    VARIABLES = ("token", "reveal")
    NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
    WRAP_WIDE = 600      # a row with the dialog to itself
    WRAP_NARROW = 470    # a row sharing its width with the Open GitHub… button

    def __init__(self, parent):
        super().__init__(parent)
        self.title("GitHub sign-in")
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False
        self.checks: queue.Queue = queue.Queue()
        self._poll_after = None

        self.token = tk.StringVar()
        self.reveal = tk.BooleanVar(value=False)
        self.source: str | None = None

        self._build()
        self._refresh_source()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())
        self._centre(parent)
        self.wait_visibility()
        self.grab_set()
        self.entry.focus_set()

    def _centre(self, parent) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 3}
        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body, wraplength=self.WRAP_WIDE, justify="left",
            text="A token lets this tool see your private repositories, and raises the "
                 "hourly limit from 60 GitHub calls to 5000.",
        ).grid(row=0, column=0, sticky="w", **pad)

        self.state_label = ttk.Label(body, wraplength=self.WRAP_WIDE, justify="left",
                                     foreground="grey")
        self.state_label.grid(row=1, column=0, sticky="w", **pad)

        ttk.Separator(body, orient="horizontal").grid(
            row=2, column=0, sticky="ew", padx=10, pady=(10, 6))

        steps = ttk.Frame(body)
        steps.grid(row=3, column=0, sticky="ew", padx=10)
        steps.columnconfigure(0, weight=1)

        ttk.Label(steps, text="Getting a token",
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(steps, text="Open GitHub…", command=self._open_github).grid(
            row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        ttk.Label(steps, wraplength=self.WRAP_NARROW, justify="left",
                  text="Open GitHub… goes straight to the page that makes one.").grid(
            row=1, column=0, sticky="w")

        # The click path spelled out, because it is genuinely hard to find and
        # the button cannot help somebody who would rather not have a program
        # open their browser. "Right at the bottom" is not padding: Developer
        # settings is the last item in a sidebar longer than the window, so it
        # is below the fold and reads as a heading rather than a link, and not
        # scrolling far enough is where people actually give up.
        ttk.Label(
            steps, wraplength=self.WRAP_WIDE, justify="left", foreground="#555555",
            text="Or by hand: your avatar (top right) → Settings → Developer settings — "
                 "the last item in the left sidebar, right at the bottom → Personal access "
                 "tokens → Fine-grained tokens → Generate new token.",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            steps, wraplength=self.WRAP_WIDE, justify="left",
            text="On the form: Repository access → Only select repositories → your addon "
                 "repository. Then Repository permissions → Contents: Read-only. Nothing "
                 "else is needed, and nothing else should be granted.",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        field = ttk.Frame(body)
        field.grid(row=4, column=0, sticky="ew", **pad)
        field.columnconfigure(1, weight=1)
        ttk.Label(field, text="Token:").grid(row=0, column=0, sticky="w")
        # Masked by default: this dialog gets opened in front of other people,
        # and a token is a password however it was generated.
        self.entry = ttk.Entry(field, textvariable=self.token, show="•", width=52)
        self.entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Checkbutton(field, text="Show", variable=self.reveal,
                        command=self._sync_reveal).grid(row=0, column=2)
        self.token.trace_add("write", self._typed)

        self.result_label = ttk.Label(body, wraplength=self.WRAP_WIDE, justify="left",
                                      foreground="grey")
        self.result_label.grid(row=5, column=0, sticky="w", **pad)

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, sticky="e", pady=(12, 0))
        self.forget_button = ttk.Button(buttons, text="Sign out", command=self._forget)
        self.forget_button.grid(row=0, column=0, padx=4)
        self.test_button = ttk.Button(buttons, text="Test", command=self._test)
        self.test_button.grid(row=0, column=1, padx=4)
        ttk.Button(buttons, text="Close", command=self._close).grid(row=0, column=2, padx=4)
        self.save_button = ttk.Button(buttons, text="Save", command=self._save)
        self.save_button.grid(row=0, column=3, padx=4)

    # -- state ---------------------------------------------------------------

    def _sync_reveal(self) -> None:
        self.entry.configure(show="" if self.reveal.get() else "•")

    def _typed(self, *_a) -> None:
        self.result_label.configure(text="", foreground="grey")
        self._sync()

    def _refresh_source(self) -> None:
        """Ask the engine where the token is coming from, and redraw.

        Called when that answer can have changed -- at open, after a save,
        after a sign-out -- and not on every keystroke: it reaches the keyring
        and `git credential fill`, so per-character it would be a subprocess
        per character on the thread drawing the box. The engine caches it, so
        only the first of these costs anything.
        """
        self.source = core.token_source()
        self._sync()

    def _sync(self) -> None:
        """Redraw the two things that depend on what is saved and what is typed."""
        source = self.source
        self.state_label.configure(text={
            None: "Not signed in. Private repositories are invisible and you have "
                  "60 GitHub calls an hour.",
            "GITHUB_TOKEN": "Signed in — using GITHUB_TOKEN from the environment. That "
                            "wins over anything saved here, and is not changed by this "
                            "window.",
            "keyring": f"Signed in — token saved in {core.secret_store_name()}.",
            "file": "Signed in — token saved in a file only you can read "
                    f"({core.tilde(str(core.token_path()))}).",
            "git": "Signed in — using the token Git or the GitHub CLI already has for "
                   "github.com. Nothing was saved by this tool.",
        }[source])

        typed = bool(self.token.get().strip())
        for button in (self.test_button, self.save_button):
            button.configure(state="normal" if typed else "disabled")
        # Sign out clears what this tool saved. It cannot unset an environment
        # variable in a parent shell, and must not pretend it can.
        removable = source in ("keyring", "file")
        self.forget_button.configure(state="normal" if removable else "disabled")

    def _say(self, message: str, *, bad: bool = False) -> None:
        self.result_label.configure(text=message, foreground="#b00020" if bad else "#0a5ea8")

    # -- actions -------------------------------------------------------------

    def _open_github(self) -> None:
        import webbrowser

        webbrowser.open(self.NEW_TOKEN_URL)
        self._say("Opened GitHub in your browser. Copy the token it gives you back here — "
                  "GitHub shows it once.")

    def _test(self) -> None:
        """Ask GitHub whose token this is, off the UI thread.

        Worth a button of its own: a token that is merely well-formed proves
        nothing, and the failure people actually hit -- a fine-grained token
        that was never granted the repository -- otherwise stays hidden until
        an install fails for what looks like an unrelated reason.
        """
        token = self.token.get().strip()
        if not token:
            return
        self.test_button.configure(state="disabled")
        self._say("Asking GitHub…")

        def ask() -> None:
            try:
                self.checks.put((core.token_identity(token), None))
            except Exception as exc:  # noqa: BLE001 - shown in the dialog
                self.checks.put((None, str(exc)))

        threading.Thread(target=ask, daemon=True).start()
        self._poll_checks()

    def _poll_checks(self) -> None:
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
        self._poll_after = self.after(100, self._drain_checks)

    def _drain_checks(self) -> None:
        self._poll_after = None
        if not self.winfo_exists():
            return
        try:
            login, error = self.checks.get_nowait()
        except queue.Empty:
            self._poll_checks()
            return
        self.test_button.configure(state="normal")
        if error:
            self._say(error, bad=True)
        else:
            self._say(f"Works — GitHub recognises this as {login}. Save it to keep it.")

    def _save(self) -> None:
        token = self.token.get().strip()
        if not token:
            return
        try:
            where = core.save_token(token)
        except Fail as exc:
            self._say(str(exc), bad=True)
            return
        self.saved = True
        self.token.set("")
        self._refresh_source()
        self._say("Saved in your system keyring." if where == "keyring"
                  else f"Saved in {core.tilde(str(core.token_path()))}, readable only by you — "
                       "this machine has no keyring for it.")

    def _forget(self) -> None:
        core.forget_token()
        self.saved = True
        self.token.set("")
        self._refresh_source()
        self._say("Signed out. The saved token has been removed.")

    def _close(self) -> None:
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
            self._poll_after = None
        held = [getattr(self, name, None) for name in self.VARIABLES]
        for name in self.VARIABLES:
            setattr(self, name, None)
        self.grab_release()
        super().destroy()
        del held


class OverwriteDialog(tk.Toplevel):
    """Asked before an install replaces an addon that is already there.

    Two questions, kept apart because the answers are not equally reversible.
    Replacing the folder is undone by installing again -- the source still has
    it. Deleting saved variables is undone by nothing: those are settings you
    made, over months, and no repository anywhere has a copy of them.

    That is why the second half sits below a rule under its own red heading,
    starts switched off, and says exactly which files it means. A destructive
    option that reads like the rest of the form is one people tick by accident.
    """

    VARIABLES = ("keep_folder", "delete_saved", "backup_saved")
    SHOWN = 6          # files listed before the list is summarised

    def __init__(self, parent, addon: str, root: Path, entry: dict):
        super().__init__(parent)
        self.title(f'Already there: "{addon}"')
        self.addon = addon
        self.destination = core.install_destination(entry, addon, root) or (root / addon)
        # A folder this tool wrote, or a link into a checkout, is not at risk:
        # replacing it loses nothing that cannot be fetched again.
        self.at_risk = (not core.is_link(self.destination)
                        and core.should_backup_folder(entry, self.destination.name))
        self.wtf = core.wtf_dir(root)
        self.saved = core.saved_variables(root, self.destination.name)
        self.result: dict | None = None
        self.transient(parent)
        self.resizable(False, False)

        self.keep_folder = tk.BooleanVar(value=True)
        self.delete_saved = tk.BooleanVar(value=False)
        # Ticked, but greyed out until deleting is asked for: if somebody does
        # turn the destructive option on, the safe answer to the next question
        # should already be the one selected.
        self.backup_saved = tk.BooleanVar(value=True)

        self._build()
        self._sync()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self._centre(parent)
        self.wait_visibility()
        self.grab_set()
        self.focus_set()

    def _centre(self, parent) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 3}
        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body, wraplength=520, justify="left",
            text=f"{self.destination.name} is already in your AddOns folder. "
                 f"Installing replaces it.",
        ).grid(row=0, column=0, sticky="w", **pad)

        if self.at_risk:
            self.keep_box = ttk.Checkbutton(
                body, variable=self.keep_folder,
                text=f"Make a backup — move it to {core.backup_name(self.destination).name} first",
            )
            self.keep_box.grid(row=1, column=0, sticky="w", **pad)
        else:
            # Nothing of yours is in there. Offering to keep a copy of it would
            # be offering to keep a copy of a download.
            self.keep_folder.set(False)
            ttk.Label(body, foreground="grey", wraplength=520, justify="left",
                      text="That folder was put there by this tool, so replacing it "
                           "loses nothing.").grid(row=1, column=0, sticky="w", **pad)

        ttk.Separator(body, orient="horizontal").grid(
            row=2, column=0, sticky="ew", padx=10, pady=(10, 2))
        ttk.Label(body, text="Delete!", foreground="#b00020",
                  font=("TkDefaultFont", 10, "bold")).grid(row=3, column=0, sticky="w", **pad)

        found = len(self.saved)
        self.delete_box = ttk.Checkbutton(
            body, variable=self.delete_saved, command=self._sync,
            text=f"Delete associated saved variables file{'' if found == 1 else 's'}"
                 + (f" ({found})" if found else ""),
        )
        self.delete_box.grid(row=4, column=0, sticky="w", **pad)

        listing = ttk.Frame(body)
        listing.grid(row=5, column=0, sticky="w", padx=(34, 10))
        for row, line in enumerate(self._lines()):
            ttk.Label(listing, text=line, foreground="grey").grid(row=row, column=0, sticky="w")

        self.backup_box = ttk.Checkbutton(
            body, variable=self.backup_saved,
            text="Backup associated saved variable files — keep a .replaced copy of each",
        )
        self.backup_box.grid(row=6, column=0, sticky="w", padx=(34, 10), pady=3)

        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Install", command=self._go).grid(row=0, column=1, padx=4)

    def _lines(self) -> list[str]:
        """The files, named. A delete nobody can see the extent of is a trap.

        Shown relative to WTF, which is where account settings and one folder
        per character are told apart -- and telling them apart is most of what
        somebody needs to know before agreeing to this.
        """
        if not self.saved:
            return ["no saved variables found for this addon"
                    + ("" if self.wtf else " — no WTF folder yet")]
        shown = [str(path.relative_to(self.wtf) if self.wtf else path)
                 for path in self.saved[:self.SHOWN]]
        if len(self.saved) > self.SHOWN:
            shown.append(f"…and {len(self.saved) - self.SHOWN} more")
        return shown

    def _sync(self, *_a) -> None:
        if self.delete_saved is None:
            return
        deleting = bool(self.delete_saved.get()) and bool(self.saved)
        self.delete_box.configure(state="normal" if self.saved else "disabled")
        self.backup_box.configure(state="normal" if deleting else "disabled")

    def _go(self) -> None:
        self.result = {
            "keep_folder": bool(self.keep_folder.get()),
            "delete_saved": bool(self.delete_saved.get()) and bool(self.saved),
            "backup_saved": bool(self.backup_saved.get()),
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def destroy(self) -> None:
        # Same rule as the other dialogs: a tk variable finalised on a worker
        # thread aborts the process rather than raising. See SourceDialog.
        held = [getattr(self, name, None) for name in self.VARIABLES]
        for name in self.VARIABLES:
            setattr(self, name, None)
        super().destroy()
        held.clear()


# ── the window ───────────────────────────────────────────────────────────────


class App(ttk.Frame):
    COLUMNS = ("source", "installed", "latest", "status")

    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        self.state = core.load()
        self.outbox: queue.Queue = queue.Queue()
        self.worker: _Worker | None = None
        self.failures = self.updated = self.outdated = self.wiped = 0
        self.wiped_kept = True
        self.checking = False
        self._reported_unloadable: set[str] = set()
        # Rows created by Install addon…, whose names are still a guess until
        # the archive is open.
        self._fresh: set[str] = set()
        # {addon: keep a copy first} for saved variables somebody asked to have
        # deleted, held until the install they belong to has succeeded.
        self._pending_saved: dict[str, bool] = {}

        # The version belongs where a user can read it off without hunting: a
        # GUI has no --version, and "which build are you running?" is the first
        # question any bug report needs answered.
        master.title(f"WoW Addons from GitHub {__version__}")
        master.minsize(760, 420)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self._build()
        self._sync_github()
        self._poll = None
        self._reschedule()
        # A first run with no WoW folder set opens straight into the picker
        # rather than an empty list, which explains nothing.
        self._first = self.after(50, self._first_run)

    def _reschedule(self) -> None:
        """Arm the next drain, cancelling any drain already armed.

        Cancelling first means _drain can also be called directly -- which the
        tests do, to avoid waiting out POLL_MS between assertions -- without
        each call leaving another timer behind.
        """
        self._cancel_after(self._poll)
        self._poll = self.after(POLL_MS, self._drain)

    def _cancel_after(self, handle) -> None:
        if handle is None:
            return
        try:
            self.after_cancel(handle)
        except tk.TclError:
            pass

    def stop(self) -> None:
        """Cancel the timers before the window goes away.

        Without this, destroy() leaves a scheduled _drain behind and Tk prints
        `invalid command name ..._drain` on the way out -- harmless, but it is
        the last thing a user sees and it reads like a crash.
        """
        self._cancel_after(self._poll)
        self._cancel_after(self._first)
        self._cancel_after(self._github_after)
        self._poll = self._first = self._github_after = None

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        # The install picker only earns its row once there is a second WoW
        # folder. Shown from the start it is a control that does nothing, which
        # is worse than absent -- _sync_installs hides and shows it.
        ttk.Label(top, text="Install:").grid(row=0, column=0, sticky="w")
        self.install_choice = tk.StringVar()
        self.install_picker = ttk.Combobox(
            top, textvariable=self.install_choice, state="readonly", width=22,
        )
        self.install_picker.grid(row=0, column=1, sticky="w", padx=8)
        self.install_picker.bind("<<ComboboxSelected>>", self._switch_install)
        self.install_row = (
            top.grid_slaves(row=0, column=0)[0], self.install_picker,
        )

        ttk.Label(top, text="WoW folder:").grid(row=1, column=0, sticky="w")
        self.folder_label = ttk.Label(top, text="(not set)", foreground="grey")
        self.folder_label.grid(row=1, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Add…", command=self.choose_folder).grid(row=1, column=2)

        # Its own row under the WoW folder, in the same shape -- label, state,
        # button -- because it is the same kind of thing: a setting that has to
        # be right before anything else in the window can work, and which is
        # otherwise invisible until something fails for a reason that does not
        # name it.
        ttk.Label(top, text="GitHub:").grid(row=2, column=0, sticky="w")
        self.github: queue.Queue = queue.Queue()
        self._github_after = None
        self._github_asked = 0
        self.github_label = ttk.Label(top, text="checking…", foreground="grey")
        self.github_label.grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Sign in…", command=self.sign_in).grid(row=2, column=2)

        table = ttk.Frame(self)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(table, columns=self.COLUMNS, selectmode="extended")
        self.tree.heading("#0", text="Addon", anchor="w")
        self.tree.heading("source", text="Source", anchor="w")
        self.tree.heading("installed", text="Installed", anchor="w")
        self.tree.heading("latest", text="Latest", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=200, minwidth=120)
        self.tree.column("source", width=280, minwidth=140)
        self.tree.column("installed", width=100, minwidth=70, anchor="w")
        self.tree.column("latest", width=100, minwidth=70, anchor="w")
        self.tree.column("status", width=170, minwidth=100)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self.set_source())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())

        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)

        # A failed row stays a row. One unreachable repo must not raise a modal
        # per failure -- with twelve addons that is twelve dialogs to dismiss.
        self.tree.tag_configure("failed", foreground="#b00020")
        self.tree.tag_configure("suggested", foreground="#0a5ea8")
        self.tree.tag_configure("busy", foreground="#666666")
        self.tree.tag_configure("unmanaged", foreground="#777777")

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.rescan_button = ttk.Button(buttons, text="Rescan", command=self.rescan)
        self.rescan_button.grid(row=0, column=0, padx=(0, 4))
        # First after Rescan, because it is the other way an addon gets into
        # the list -- and the only one that does not require having installed
        # it by hand already.
        self.install_button = ttk.Button(buttons, text="Install addon…", command=self.install_addon)
        self.install_button.grid(row=0, column=1, padx=4)
        self.source_button = ttk.Button(buttons, text="Set source…", command=self.set_source)
        self.source_button.grid(row=0, column=2, padx=4)
        self.accept_button = ttk.Button(buttons, text="Accept suggestion", command=self.accept_suggestion)
        self.accept_button.grid(row=0, column=3, padx=4)
        self.check_button = ttk.Button(buttons, text="Check for updates", command=self.check_all)
        self.check_button.grid(row=0, column=4, padx=4)
        self.update_button = ttk.Button(buttons, text="Update selected", command=self.update_selected)
        self.update_button.grid(row=0, column=5, padx=4)
        self.update_all_button = ttk.Button(buttons, text="Update all", command=self.update_all)
        self.update_all_button.grid(row=0, column=6, padx=4)
        self.cancel_button = ttk.Button(buttons, text="Stop", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=7, padx=4)

        # Its own row: the caption is a sentence, because the trade it makes is
        # not guessable from a three-word label. A checkbox that quietly stops
        # an addon following its releases has to say so where it is ticked.
        self.no_api = tk.BooleanVar(value=False)
        self.no_api_box = ttk.Checkbutton(
            buttons,
            text="Check without the GitHub API — no rate limit, but follows branches "
                 "instead of releases and downloads more",
            variable=self.no_api,
            command=self._toggle_no_api,
        )
        self.no_api_box.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        status = ttk.Frame(self)
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status = ttk.Label(status, text="Ready.")
        self.status.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="determinate", length=200)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.counter = ttk.Label(status, text="")
        self.counter.grid(row=0, column=2, sticky="e", padx=(8, 0))

    # -- state ---------------------------------------------------------------

    def _first_run(self) -> None:
        if not self.install().get("addons_dir"):
            self.say("Point this at your WoW folder to begin.")
            self.choose_folder()
        else:
            self.refresh()

    def say(self, message: str) -> None:
        self.status.configure(text=message)

    def _sync_installs(self) -> None:
        """Keep the picker showing what the manifest holds.

        Hidden while there is one install, because a dropdown with a single
        entry is a control that cannot do anything.
        """
        known = sorted(core.installs(self.state), key=str.lower)
        current = core.current_name(self.state) if known else ""
        self.install_picker.configure(values=known)
        if self.install_choice.get() != current:
            self.install_choice.set(current)
        for widget in self.install_row:
            if len(known) > 1:
                widget.grid()
            else:
                widget.grid_remove()

    def _switch_install(self, *_a) -> None:
        chosen = self.install_choice.get()
        if not chosen or chosen == self.state.get("current"):
            return
        if self.guard(lambda: core.use(self.state, chosen)) is None:
            return
        core.save(self.state)
        self.refresh()
        self.say(f"{chosen}: {core.tilde(self.install().get('addons_dir') or '(not set)')}")

    def install(self) -> dict:
        """The WoW folder every button on this window acts on.

        One person can have a vanilla server, a Wrath one and retail; they
        share no addons and an addon bound in one says nothing about the same
        addon in another. The picker above the table chooses between them.
        """
        return core.current(self.state)

    def entries(self) -> dict:
        return self.install().setdefault("addons", {})

    def root_dir(self) -> Path | None:
        directory = self.install().get("addons_dir")
        return Path(directory) if directory else None

    def refresh(self) -> None:
        """Redraw the table from the manifest. Never touches the disk or network."""
        directory = self.install().get("addons_dir")
        self._sync_installs()
        # Per install, so a vanilla server behind a shared address and a retail
        # one at home do not have to agree about it.
        self.no_api.set(core.checks_without_api(self.install()))
        self.folder_label.configure(
            text=core.tilde(directory) if directory else "(not set)",
            foreground="" if directory else "grey",
        )

        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        entries = self.entries()
        for name in core.display_order(entries):
            entry = entries[name]
            source = entry.get("source", "unmanaged")
            tags = []
            status = ""
            if entry.get("missing"):
                status = "not installed"
            if source == "unmanaged":
                tags.append("unmanaged")
                if entry.get("suggested"):
                    status = f"suggests {entry['suggested']}"
                    tags = ["suggested"]
            elif core.covers_several_addons(entry):
                # This row installs a whole repository of addons. It works, and
                # it is usually not what was meant: every addon in the repo is
                # written into AddOns whenever this one updates, and each of
                # them reports an update whenever any of them changes.
                #
                # The engine already says so in a note, and the note goes into
                # the Status column, which is 170 pixels wide -- a sentence
                # there is a smudge, not a warning. A short flag is readable,
                # and Set source now opens on the list to fix it with.
                status = status or f"installs {len(entry['folders'])} addons"
                tags.append("suggested")
            self.tree.insert(
                "", "end", iid=name, text=name,
                values=(
                    UNMANAGED_LABEL if source == "unmanaged" else core.tilde(source),
                    entry.get("installed") or entry.get("toc_version") or "",
                    # Remembered from the last check, so the column is not blank
                    # every time the window opens.
                    entry.get("latest") or "",
                    status,
                ),
                tags=tags,
            )
        for name in selected:
            if self.tree.exists(name):
                self.tree.selection_add(name)
        self._sync_buttons()

    def _toggle_no_api(self) -> None:
        core.set_checks_without_api(self.install(), self.no_api.get())
        core.save(self.state)
        self.say(
            "Checking without the GitHub API. Bound addons follow their default "
            "branch; the first check of one may download its repository."
            if self.no_api.get() else
            "Checking through the GitHub API again."
        )

    def _sync_buttons(self) -> None:
        running = self.worker is not None and self.worker.is_alive()
        one = "normal" if len(self.tree.selection()) >= 1 and not running else "disabled"
        entries = self.entries()
        suggests = any(entries.get(n, {}).get("suggested") for n in self.tree.selection())
        for button, state in (
            (self.rescan_button, "disabled" if running else "normal"),
            (self.install_button, "disabled" if running else "normal"),
            (self.check_button, "disabled" if running else "normal"),
            (self.source_button, one),
            (self.accept_button, "normal" if suggests and not running else "disabled"),
            (self.update_button, one),
            (self.update_all_button, "disabled" if running else "normal"),
            (self.cancel_button, "normal" if running else "disabled"),
            (self.no_api_box, "disabled" if running else "normal"),
        ):
            button.configure(state=state)

    def selection(self) -> list[str]:
        return list(self.tree.selection())

    # -- actions -------------------------------------------------------------

    def guard(self, action):
        """Run something that may fail, and put the reason in a dialog.

        Fail is the expected kind: a repo that cannot be reached, a folder that
        is not there. Anything else is a bug in this program -- but a windowed
        build has no console, so an unhandled exception in a button callback
        goes nowhere at all. The button then appears to do nothing, and the
        table redraws the old value, which reads as the app quietly undoing
        what you asked for.

        That is not hypothetical: v0.5.0 shipped with `Set source` raising
        TypeError on every use, and it was reported as "it reverts back to the
        source instead of leaving it unmanaged". Showing the error would not
        have fixed the bug, but it would have named it.
        """
        try:
            return action()
        except Fail as exc:
            messagebox.showerror("Cannot do that", str(exc), parent=self)
            return None
        except Exception as exc:  # noqa: BLE001 - see above; nowhere else to report
            traceback.print_exc()
            messagebox.showerror(
                "Something went wrong",
                f"{type(exc).__name__}: {exc}\n\n"
                "This is a bug in this program, not something you did.\n"
                f"Please report it at {ISSUES_URL}",
                parent=self,
            )
            return None

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Your WoW folder, or Interface/AddOns", parent=self)
        if not chosen:
            self.refresh()
            return
        target = self.guard(lambda: core.find_addons_dir(Path(chosen)))
        if target is None:
            return
        name = core.add_install(self.state, target)
        core.save(self.state)
        self.say(f"Reading {target}…")
        self.rescan()
        self.say(f"{name}: {core.tilde(str(target))}")

    def sign_in(self) -> None:
        """Open the token dialog, and redraw once it closes.

        The label is the only thing that has to change here. What a token
        actually affects -- which repositories are visible -- is not known
        until something is asked of GitHub, and re-checking every addon because
        somebody opened this dialog would be an expensive surprise.
        """
        dialog = SignInDialog(self)
        self.wait_window(dialog)
        self._sync_github()
        if getattr(dialog, "saved", False):
            self.say("GitHub sign-in changed. Check for updates to use it.")

    LABELS = {
        None: "not signed in — public repositories only, 60 calls an hour",
        "GITHUB_TOKEN": "signed in (GITHUB_TOKEN)",
        "keyring": "signed in (saved in this machine's secret store)",
        "file": "signed in (saved on this machine)",
        "git": "signed in (using Git's saved GitHub login)",
    }

    def _sync_github(self) -> None:
        """Say whether a token is in play, and where it came from.

        Where it came from matters: somebody who signed in here and is still
        being told a repository does not exist needs to know whether the token
        being sent is the one they just saved or one Git had all along.

        Off the UI thread, because working that out means asking the system
        keyring and `git credential fill`, and both are subprocesses. On the
        main thread the first one would run before the window had drawn -- a
        startup that appears to hang for as long as somebody's credential
        helper takes to think about it. The answer is cached in the engine, so
        this is slow once and instant afterwards.
        """
        # Back to "checking…" first: this is called again after the sign-in
        # dialog closes, and until the worker answers, the label on screen is
        # the state from before that dialog -- which is exactly the state the
        # person has just changed.
        self.github_label.configure(text="checking…", foreground="grey")

        # Numbered, because the answer to the previous ask may still be in
        # flight -- the window asks once at startup and again when the sign-in
        # dialog closes, and the startup answer landing second would redraw the
        # label with the state from before the person signed in. Same reason
        # the repository lookups carry the spec they were asked about.
        self._github_asked += 1
        asking = self._github_asked

        def look() -> None:
            try:
                self.github.put((asking, core.token_source()))
            except Exception:  # noqa: BLE001 - a label is not worth a crash
                self.github.put((asking, None))

        threading.Thread(target=look, daemon=True).start()
        self._poll_github()

    def _poll_github(self) -> None:
        if self._github_after is not None:
            self.after_cancel(self._github_after)
        self._github_after = self.after(80, self._drain_github)

    def _drain_github(self) -> None:
        self._github_after = None
        source = None
        answered = False
        while True:
            try:
                asked, found = self.github.get_nowait()
            except queue.Empty:
                break
            if asked == self._github_asked:
                source, answered = found, True
        if not answered:
            self._poll_github()
            return
        self.github_label.configure(
            text=self.LABELS[source],
            foreground="grey" if source is None else "#0a5ea8",
        )

    def rescan(self) -> None:
        root = self.root_dir()
        if root is None:
            self.say("No WoW folder set yet.")
            return
        install = self.install()
        outcome = self.guard(lambda: core.rescan(install, core.addons_dir(install)))
        if outcome is None:
            return
        installed, guessed, forgotten = outcome
        core.save(self.state)
        self.refresh()
        gone = f" {forgotten} deleted addon(s) dropped." if forgotten else ""
        problems = self._unloadable(core.addons_dir(install))
        skipped = f" {len(problems)} folder(s) the game cannot load." if problems else ""
        self.say(
            f"{installed} addon folder(s); {guessed} with a source found or suggested."
            f"{gone}{skipped}"
        )
        self._report_unloadable(problems)

    def _unloadable(self, root: Path) -> dict:
        """Folders that hold an addon and still will not load. Never fatal."""
        try:
            return core.scan_problems(root)
        except OSError:
            return {}

    def _report_unloadable(self, problems: dict) -> None:
        """Say out loud why a folder that is plainly there is not in the list.

        Being told "28 addon folder(s)" when you can see 29 in your file manager
        reads as the scan being broken. It is usually a folder the game will not
        load either -- a .toc named after the wrong thing, an addon left one
        level deep inside its zip's folder -- and naming the fix is the whole
        point of noticing.

        Once per distinct set of folders: this repeats on every rescan otherwise,
        and a folder of parked addons is a thing people keep on purpose.
        """
        if not problems or set(problems) == self._reported_unloadable:
            self._reported_unloadable = set(problems)
            return
        self._reported_unloadable = set(problems)
        lines = "\n\n".join(f"{name}\n    {why}" for name, why in problems.items())
        messagebox.showinfo(
            "Folders the game will not load",
            "These are in your AddOns folder, but World of Warcraft does not load "
            "them, so they are not in the list:\n\n" + lines,
            parent=self,
        )

    def set_source(self) -> None:
        names = self.selection()
        if not names:
            return
        root = self.root_dir()
        if root is None:
            self.say("Set your WoW folder first.")
            return
        name = names[0]
        entry = self.entries().setdefault(name, core.new_entry(name))
        dialog = SourceDialog(self.master, name, entry, root,
                              no_api=core.checks_without_api(self.install()))
        self.master.wait_window(dialog)
        if dialog.result is None:
            return
        source, copy = dialog.result
        keep = dialog.keep_backup
        if self.guard(lambda: core.set_source(self.install(), name, source, copy=copy, backup=keep)) is None:
            return
        core.save(self.state)
        self.refresh()
        self.say(f"{name} -> {core.tilde(self.entries()[name]['source'])}")

    def install_addon(self) -> None:
        """Install something that is not in AddOns yet, from a pasted repository.

        Binding and installing are one action here on purpose. Every other row
        in this table describes a folder that already exists; a row created by
        this button describes one that does not, and stopping at the binding
        would leave the list asserting an addon is installed when nothing has
        been downloaded.
        """
        root = self.root_dir()
        if root is None:
            self.say("Set your WoW folder first.")
            return
        if self.guard(lambda: core.addons_dir(self.install())) is None:
            return
        dialog = InstallDialog(self.master, root, self.entries(),
                               no_api=core.checks_without_api(self.install()))
        self.master.wait_window(dialog)
        if not dialog.result:
            return

        wanted = []
        for name, source in dialog.result:
            decision = self._confirm_overwrite(name, source, root)
            if decision is None:
                continue  # this one was declined; anything else ticked still goes
            if self.guard(lambda n=name, s=source, d=decision:
                          core.set_source(self.install(), n, s, backup=d["keep_folder"])) is None:
                return
            if decision["delete_saved"]:
                # Acted on after the install, not now: a download that fails
                # must not take somebody's settings with it.
                self._pending_saved[name] = decision["backup_saved"]
            wanted.append(name)
        if not wanted:
            self.say("Nothing installed.")
            return
        core.save(self.state)
        self.refresh()
        # The rows exist but hold nothing yet, so their names are provisional:
        # what the archive turns out to contain decides what they are called.
        self._fresh.update(wanted)
        self.start(wanted)

    def _confirm_overwrite(self, name: str, source: str, root: Path) -> dict | None:
        """Ask before replacing a folder that is already in AddOns. None = don't.

        Only when there is something to replace. An install that lands in empty
        space has nothing to confirm, and a dialog that appears every time
        teaches people to click through the one that matters.
        """
        entry = dict(self.entries().get(name) or core.new_entry(name))
        entry["source"] = source
        destination = core.install_destination(entry, name, root)
        if destination is None or not destination.exists():
            return {"keep_folder": entry.get("backup", True),
                    "delete_saved": False, "backup_saved": True}
        dialog = OverwriteDialog(self.master, name, root, entry)
        self.master.wait_window(dialog)
        return dialog.result

    def accept_suggestion(self) -> None:
        """Take the .toc's suggestion for the selected rows, on an explicit click.

        Suggestions are shown, never applied: `scan` guesses a source from a
        header the addon's author wrote, which is a claim about where the code
        lives, not this user's decision to install from there.
        """
        taken = 0
        for name in self.selection():
            entry = self.entries().get(name, {})
            if entry.get("source") == "unmanaged" and entry.get("suggested"):
                entry["source"] = entry.pop("suggested")
                taken += 1
        if taken:
            core.save(self.state)
            self.refresh()
        self.say(f"{taken} source(s) accepted." if taken else "Nothing suggested for that selection.")

    def update_selected(self) -> None:
        self.start(self.selection())

    def update_all(self) -> None:
        self.start(core.display_order(self.entries()))

    def check_all(self) -> None:
        """Ask every bound addon what the latest version is. Download nothing.

        The point of a separate button: seeing what is out of date should not
        commit you to installing it, and on a slow connection an unwanted
        `Update all` is not something you can take back.
        """
        self.start(core.display_order(self.entries()), check=True)

    def start(self, names: list[str], *, check: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        root = self.root_dir()
        if root is None:
            self.say("Set your WoW folder first.")
            return
        if self.guard(lambda: core.addons_dir(self.install())) is None:
            return
        names = [n for n in names if self.entries().get(n, {}).get("source", "unmanaged") != "unmanaged"]
        if not names:
            self.say("Nothing selected has a source set.")
            return

        for name in names:
            if self.tree.exists(name):
                self.tree.set(name, "status", "waiting")
                self.tree.item(name, tags=["busy"])
        self.failures = 0
        self.updated = 0
        self.checking = check
        self.outdated = 0
        self.wiped = 0
        self.wiped_kept = True
        self.progress.configure(maximum=len(names), value=0)
        self.counter.configure(text=f"0/{len(names)}")
        self.say("Checking…" if check else "Working…")

        core.begin_run()
        self.worker = _Worker(names, self.entries(), root, self.outbox, check=check,
                              no_api=core.checks_without_api(self.install()))
        self.worker.start()
        self._sync_buttons()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancelled.set()
            self.say("Stopping after this addon…")

    # -- draining the worker -------------------------------------------------

    def _drain(self) -> None:
        """The only place a worker's output reaches a widget."""
        try:
            while True:
                kind, payload = self.outbox.get_nowait()
                if kind == "begin":
                    name, index, total = payload
                    self.counter.configure(text=f"{index}/{total}")
                    if self.tree.exists(name):
                        self.tree.set(name, "status", "checking…")
                elif kind == "progress":
                    name, stage, detail = payload
                    if self.tree.exists(name):
                        self.tree.set(name, "status", f"{stage}…")
                elif kind == "waiting":
                    name, seconds, why = payload
                    if self.tree.exists(name):
                        self.tree.set(name, "status", f"waiting {seconds:.0f}s…")
                    self.say(f"Waiting {seconds:.0f}s — {why}")
                elif kind == "result":
                    self._show_result(payload)
                elif kind == "cancelled":
                    self.say("Stopped.")
                elif kind == "done":
                    self._finished()
        except queue.Empty:
            pass
        except Exception as exc:  # never let the poll loop die
            self.say(f"internal error while updating the list: {exc}")
        finally:
            self._reschedule()

    def _settled_name(self, result: core.Result) -> str:
        """The name a just-installed row should be listed under.

        An install has to name its row before it can know what the archive
        holds: the repository's name is the best guess available, and a
        repository called NotPlater-3.3.5 whose addon folder is NotPlater makes
        that guess wrong. Once the folder is on disk there is no need to keep
        guessing -- and leaving it wrong would show the addon twice, as a bound
        row that reads "not installed" beside the unmanaged row the next rescan
        adds for the folder that is actually there.
        """
        if result.name not in self._fresh or result.failed:
            return result.name
        self._fresh.discard(result.name)
        settled = core.settle_names(self.install(), [result.name])
        if not settled:
            return result.name
        core.save(self.state)
        self._redraw_keeping_status()
        return settled[0][1]

    def _delete_saved_variables(self, requested: str, result: core.Result) -> None:
        """Carry out the delete the confirm dialog agreed to, once and afterwards.

        Afterwards, because settings are the one thing here that no source can
        fetch again: if the download failed, the old addon is still installed
        and wiping what it remembers would be a loss with nothing gained. The
        request is dropped either way -- it belonged to that install, not to
        whatever this row does next.
        """
        keep = self._pending_saved.pop(requested, None)
        root = self.root_dir()
        if keep is None or root is None or result.failed or self.checking:
            return
        deleted, problems = core.remove_saved_variables(
            core.saved_variables(root, result.name), backup=keep
        )
        # Counted for the run summary rather than written on the row: the row
        # says "recently updated", and a delete somebody asked for still
        # deserves one acknowledgement that it happened.
        self.wiped += len(deleted)
        self.wiped_kept = self.wiped_kept and keep
        for problem in problems:
            result.notes.append(("warn", f"saved variables: {problem}"))

    def _redraw_keeping_status(self) -> None:
        """Rebuild the table without wiping what this run has said on each row.

        refresh() draws from the manifest, which knows nothing about "waiting"
        or "downloading…" -- so a mid-run redraw would blank the progress of
        every other addon in the same run.
        """
        held = {name: (self.tree.set(name, "status"), self.tree.item(name, "tags"))
                for name in self.tree.get_children()}
        self.refresh()
        for name, (status, tags) in held.items():
            if status and self.tree.exists(name):
                self.tree.set(name, "status", status)
                self.tree.item(name, tags=list(tags))

    def _show_result(self, result: core.Result) -> None:
        self.progress.configure(value=self.progress["value"] + 1)
        requested = result.name
        result = replace(result, name=self._settled_name(result))
        self._delete_saved_variables(requested, result)
        entry = self.entries().get(result.name, {})

        # Remember what the source said, so Latest survives closing the window.
        if result.version and not result.failed:
            entry["latest"] = result.version

        if self.tree.exists(result.name):
            self.tree.set(result.name, "source", core.tilde(entry.get("source", "unmanaged")))
            self.tree.set(result.name, "installed", entry.get("installed") or "")
            self.tree.set(result.name, "latest", entry.get("latest") or "")
            if result.failed:
                # Per-row failures stay on their row.
                self.tree.set(result.name, "status", result.detail.splitlines()[0])
                self.tree.item(result.name, tags=["failed"])
            elif result.outcome == core.UP_TO_DATE:
                self.tree.set(result.name, "status", "up to date")
                self.tree.item(result.name, tags=[])
            elif self.checking:
                # A check reports; it does not install. Saying "updated" here
                # would be a lie the Installed column immediately contradicts.
                self.tree.set(result.name, "status", f"update available: {result.version}")
                self.tree.item(result.name, tags=["suggested"])
            else:
                self.tree.set(result.name, "status", self._settled_status(result, entry))
                self.tree.item(result.name, tags=self._settled_tags(result, entry))

        if result.failed:
            self.failures += 1
        elif result.outcome == core.CHANGED:
            if self.checking:
                self.outdated += 1
            else:
                self.updated += 1

    def _settled_status(self, result: core.Result, entry: dict) -> str:
        """What the Status column says about an addon that just installed.

        "recently updated", not a sentence about what happened on the way
        there. The engine narrates each step it takes -- a folder moved aside,
        settings deleted -- and every one of those was asked for a moment
        earlier in the confirm dialog. Replaying it in a 170-pixel column
        crowded out the one thing this column is scanned for, which is which
        rows just changed.

        Two things still outrank it, because neither is something the person
        already knows:

          a warning -- something that did NOT go to plan, inside a run that
          otherwise succeeded, like a settings file that would not delete

          a row that installs a whole repository, which is the same flag
          refresh() puts there and is rarely what was meant
        """
        warnings = "; ".join(message for level, message in result.notes if level == "warn")
        if warnings:
            return warnings
        if core.covers_several_addons(entry):
            return f"installs {len(entry['folders'])} addons"
        return "recently updated"

    def _settled_tags(self, result: core.Result, entry: dict) -> list[str]:
        wrong = any(level == "warn" for level, _m in result.notes)
        return ["suggested"] if wrong or core.covers_several_addons(entry) else []

    def _finished(self) -> None:
        # Saved either way: a check writes no addon files, but the versions it
        # learned are worth keeping so the Latest column is not blank next time.
        core.save(self.state)
        # The ETags this run learned are what make the next one cost nothing,
        # so they are kept whether or not anything was installed.
        core.end_run()
        self.worker = None
        self.counter.configure(text="")
        self.progress.configure(value=0)
        tail = f", {self.failures} failed" if self.failures else ""
        # What is left of the hour, when we have been told. Seeing the number
        # fall is how somebody learns that pinning a branch, or binding the
        # addon they are working on locally, costs nothing at all.
        left = core.quota_left()
        budget = f" {left} GitHub call(s) left this hour." if left is not None else ""

        if self.checking:
            found = f"{self.outdated} update(s) available" if self.outdated else "everything is up to date"
            self.say(f"Checked — {found}{tail}. Nothing was downloaded.{budget}")
        else:
            done = f"Done — {self.updated} updated{tail}."
            wiped = ""
            if self.wiped:
                wiped = (f" {self.wiped} saved variables file(s) deleted"
                         + (" — copies kept beside them." if self.wiped_kept
                            else " — no copies kept."))
            self.say(done + wiped
                     + (" Restart the client, or /reload." if self.updated else "") + budget)
        self._sync_buttons()


def main() -> None:
    root = tk.Tk()
    try:
        # 'clam' is the least dated theme that ships everywhere, including
        # inside the AppImage's bundled Tk.
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = App(root)

    def close():
        app.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
