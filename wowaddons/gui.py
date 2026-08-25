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
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, core
from .core import Fail

POLL_MS = 100
UNMANAGED_LABEL = "(unmanaged)"


# ── the worker ───────────────────────────────────────────────────────────────


class _Worker(threading.Thread):
    """Runs updates off the UI thread and reports back through a queue.

    Deliberately knows nothing about Tk. It takes the entries it was given,
    calls core.update_addon on each, and posts the results; the window decides
    what any of that looks like.
    """

    def __init__(self, names, entries, root, outbox, *, force=False, check=False):
        super().__init__(daemon=True)
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

                result = core.update_addon(
                    name, entry, self.root, force=self.force, check=self.check, progress=progress
                )
                self.outbox.put(("result", result))
        finally:
            self.outbox.put(("done", None))


# ── the set-source dialog ────────────────────────────────────────────────────


class SourceDialog(tk.Toplevel):
    """Where one addon's source is chosen. Returns (source, copy) or None.

    The displacement warning lives here rather than after Save on purpose: this
    is the confirm step, and moving a real folder aside is the one thing this
    tool does that cannot be undone. In a terminal you read about it in the log
    afterwards; in a window there is no log, so it has to be said in advance.
    """

    # Every tk variable this dialog owns. Named once, so destroy() cannot drift
    # out of step with __init__.
    VARIABLES = ("choice", "local", "repo", "branch", "track", "copy", "backup", "folder")

    def __init__(self, parent, addon: str, entry: dict, root: Path):
        super().__init__(parent)
        self.title(f'Source for "{addon}"')
        self.addon = addon
        self.addons_root = root
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

    def _centre(self, parent) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

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

        self.caution = ttk.Label(body, text="", foreground="#a05000", wraplength=480, justify="left")
        self.caution.grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=1, padx=4)
        body.columnconfigure(1, weight=1)

    def _absorb_url(self, *_a) -> None:
        """Show what a pasted URL was understood as, while it is being pasted.

        Silently accepting a URL and only revealing the interpretation after
        Save leaves somebody guessing whether it took the branch they meant.
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
        if folder and not self.folder.get().strip():
            # Clicking into one addon of several and copying the address is the
            # clearest way anybody states which addon they mean. Keep it.
            self.folder.set(folder)
        shown = f"→ {repo}"
        if branch:
            shown += f" @ {branch}"
        if folder:
            shown += f" · {folder}"
        self.repo_hint.configure(text=shown)

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
        held = [getattr(self, name, None) for name in self.VARIABLES]
        for name in self.VARIABLES:
            setattr(self, name, None)
        super().destroy()
        held.clear()  # __del__ runs now, on this thread, with Tcl still up


# ── the window ───────────────────────────────────────────────────────────────


class App(ttk.Frame):
    COLUMNS = ("source", "installed", "latest", "status")

    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        self.state = core.load()
        self.outbox: queue.Queue = queue.Queue()
        self.worker: _Worker | None = None
        self.failures = self.updated = self.outdated = 0
        self.checking = False

        # The version belongs where a user can read it off without hunting: a
        # GUI has no --version, and "which build are you running?" is the first
        # question any bug report needs answered.
        master.title(f"WoW Addons from GitHub {__version__}")
        master.minsize(760, 420)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self._build()
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
        self._poll = self._first = None

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="WoW folder:").grid(row=0, column=0, sticky="w")
        self.folder_label = ttk.Label(top, text="(not set)", foreground="grey")
        self.folder_label.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Change…", command=self.choose_folder).grid(row=0, column=2)

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
        self.source_button = ttk.Button(buttons, text="Set source…", command=self.set_source)
        self.source_button.grid(row=0, column=1, padx=4)
        self.accept_button = ttk.Button(buttons, text="Accept suggestion", command=self.accept_suggestion)
        self.accept_button.grid(row=0, column=2, padx=4)
        self.check_button = ttk.Button(buttons, text="Check for updates", command=self.check_all)
        self.check_button.grid(row=0, column=3, padx=4)
        self.update_button = ttk.Button(buttons, text="Update selected", command=self.update_selected)
        self.update_button.grid(row=0, column=4, padx=4)
        self.update_all_button = ttk.Button(buttons, text="Update all", command=self.update_all)
        self.update_all_button.grid(row=0, column=5, padx=4)
        self.cancel_button = ttk.Button(buttons, text="Stop", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=6, padx=4)

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
        if not self.state.get("addons_dir"):
            self.say("Point this at your WoW folder to begin.")
            self.choose_folder()
        else:
            self.refresh()

    def say(self, message: str) -> None:
        self.status.configure(text=message)

    def entries(self) -> dict:
        return self.state.setdefault("addons", {})

    def root_dir(self) -> Path | None:
        directory = self.state.get("addons_dir")
        return Path(directory) if directory else None

    def refresh(self) -> None:
        """Redraw the table from the manifest. Never touches the disk or network."""
        directory = self.state.get("addons_dir")
        self.folder_label.configure(
            text=core.tilde(directory) if directory else "(not set)",
            foreground="" if directory else "grey",
        )

        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        entries = self.entries()
        for name in sorted(entries, key=str.lower):
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

    def _sync_buttons(self) -> None:
        running = self.worker is not None and self.worker.is_alive()
        one = "normal" if len(self.tree.selection()) >= 1 and not running else "disabled"
        entries = self.entries()
        suggests = any(entries.get(n, {}).get("suggested") for n in self.tree.selection())
        for button, state in (
            (self.rescan_button, "disabled" if running else "normal"),
            (self.check_button, "disabled" if running else "normal"),
            (self.source_button, one),
            (self.accept_button, "normal" if suggests and not running else "disabled"),
            (self.update_button, one),
            (self.update_all_button, "disabled" if running else "normal"),
            (self.cancel_button, "normal" if running else "disabled"),
        ):
            button.configure(state=state)

    def selection(self) -> list[str]:
        return list(self.tree.selection())

    # -- actions -------------------------------------------------------------

    def guard(self, action):
        """Run something that may raise Fail, and put the message in a dialog."""
        try:
            return action()
        except Fail as exc:
            messagebox.showerror("Cannot do that", str(exc), parent=self)
            return None

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Your WoW folder, or Interface/AddOns", parent=self)
        if not chosen:
            self.refresh()
            return
        target = self.guard(lambda: core.find_addons_dir(Path(chosen)))
        if target is None:
            return
        self.state["addons_dir"] = str(target)
        core.save(self.state)
        self.say(f"Reading {target}…")
        self.rescan()

    def rescan(self) -> None:
        root = self.root_dir()
        if root is None:
            self.say("No WoW folder set yet.")
            return
        outcome = self.guard(lambda: core.rescan(self.state, core.addons_dir(self.state)))
        if outcome is None:
            return
        installed, guessed = outcome
        core.save(self.state)
        self.refresh()
        self.say(f"{installed} addon folder(s); {guessed} with a source found or suggested.")

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
        dialog = SourceDialog(self.master, name, entry, root)
        self.master.wait_window(dialog)
        if dialog.result is None:
            return
        source, copy = dialog.result
        keep = dialog.keep_backup
        if self.guard(lambda: core.set_source(self.state, name, source, copy=copy, backup=keep)) is None:
            return
        core.save(self.state)
        self.refresh()
        self.say(f"{name} -> {core.tilde(self.entries()[name]['source'])}")

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
        self.start(sorted(self.entries(), key=str.lower))

    def check_all(self) -> None:
        """Ask every bound addon what the latest version is. Download nothing.

        The point of a separate button: seeing what is out of date should not
        commit you to installing it, and on a slow connection an unwanted
        `Update all` is not something you can take back.
        """
        self.start(sorted(self.entries(), key=str.lower), check=True)

    def start(self, names: list[str], *, check: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        root = self.root_dir()
        if root is None:
            self.say("Set your WoW folder first.")
            return
        if self.guard(lambda: core.addons_dir(self.state)) is None:
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
        self.progress.configure(maximum=len(names), value=0)
        self.counter.configure(text=f"0/{len(names)}")
        self.say("Checking…" if check else "Working…")

        self.worker = _Worker(names, self.entries(), root, self.outbox, check=check)
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

    def _show_result(self, result: core.Result) -> None:
        self.progress.configure(value=self.progress["value"] + 1)
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
                note = "; ".join(m for _l, m in result.notes)
                self.tree.set(result.name, "status", note or "updated")
                self.tree.item(result.name, tags=[])

        if result.failed:
            self.failures += 1
        elif result.outcome == core.CHANGED:
            if self.checking:
                self.outdated += 1
            else:
                self.updated += 1

    def _finished(self) -> None:
        # Saved either way: a check writes no addon files, but the versions it
        # learned are worth keeping so the Latest column is not blank next time.
        core.save(self.state)
        self.worker = None
        self.counter.configure(text="")
        self.progress.configure(value=0)
        tail = f", {self.failures} failed" if self.failures else ""

        if self.checking:
            found = f"{self.outdated} update(s) available" if self.outdated else "everything is up to date"
            self.say(f"Checked — {found}{tail}. Nothing was downloaded.")
        else:
            done = f"Done — {self.updated} updated{tail}."
            self.say(done + (" Restart the client, or /reload." if self.updated else ""))
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
