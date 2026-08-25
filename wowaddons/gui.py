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

from . import core
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

    def __init__(self, parent, addon: str, entry: dict, root: Path):
        super().__init__(parent)
        self.title(f'Source for "{addon}"')
        self.addon = addon
        self.addons_root = root
        self.result: tuple[str, bool] | None = None
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

        if source.startswith("local:"):
            self.choice.set("local")
            self.local.set(source[len("local:"):])
        elif source.startswith("github:"):
            self.choice.set("github")
            rest = source[len("github:"):]
            if "@" in rest:
                repo, branch = rest.split("@", 1)
                self.repo.set(repo)
                self.branch.set(branch)
                self.track.set(True)
            else:
                self.repo.set(rest)
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
        ttk.Label(body, text="owner/repo", foreground="grey").grid(row=2, column=2, sticky="w", **pad)

        track = ttk.Frame(body)
        track.grid(row=3, column=1, sticky="w", **pad)
        self.track_box = ttk.Checkbutton(track, text="track branch:", variable=self.track, command=self._sync)
        self.track_box.grid(row=0, column=0, sticky="w")
        self.branch_entry = ttk.Entry(track, textvariable=self.branch, width=18)
        self.branch_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Radiobutton(body, text="Leave unmanaged", value="unmanaged", variable=self.choice,
                        command=self._sync).grid(row=4, column=0, sticky="w", **pad)

        if suggested:
            ttk.Label(body, text=f"This addon's .toc suggests {suggested}", foreground="grey").grid(
                row=5, column=0, columnspan=3, sticky="w", **pad)

        self.caution = ttk.Label(body, text="", foreground="#a05000", wraplength=460, justify="left")
        self.caution.grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=1, padx=4)
        body.columnconfigure(1, weight=1)

    def _sync(self, *_a) -> None:
        """Grey out whatever the current choice does not use."""
        choice = self.choice.get()
        local = "normal" if choice == "local" else "disabled"
        github = "normal" if choice == "github" else "disabled"
        self.local_entry.configure(state=local)
        self.browse.configure(state=local)
        self.copy_box.configure(state=local)
        self.repo_entry.configure(state=github)
        self.track_box.configure(state=github)
        self.branch_entry.configure(state="normal" if choice == "github" and self.track.get() else "disabled")
        self._show_caution()

    def _displaced(self) -> tuple[str, Path] | None:
        """(folder name, where it would be moved to), or None if nothing is at risk.

        The folder that actually gets displaced is named after the SOURCE, not
        after the addon: binding "OldThing" to a checkout called OldThing-fork
        installs OldThing-fork, because the client matches folder name to the
        .toc inside and renaming on the way in would break it. Warning about
        the wrong folder would be worse than not warning at all.
        """
        choice = self.choice.get()
        if choice == "unmanaged":
            return None
        if choice == "local":
            path = self.local.get().strip()
            if not path:
                return None
            backup = core.will_displace({"source": f"local:{path}"}, self.addons_root)
            return (Path(path).name, backup) if backup else None

        # github: which folders the archive contains is not knowable until it
        # has been downloaded, so the addon's own name is the best guess going.
        existing = self.addons_root / self.addon
        if existing.exists() and not core.is_link(existing):
            return self.addon, core.backup_name(existing)
        return None

    def _show_caution(self, *_a) -> None:
        displaced = self._displaced()
        if displaced is None:
            self.caution.configure(text="")
            return
        name, backup = displaced
        self.caution.configure(
            text=f"⚠  {name} is real files in your AddOns folder right now. "
                 f"Updating it will move that folder aside to {backup.name} rather than delete it."
        )

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title=f"Folder holding {self.addon}", parent=self)
        if chosen:
            self.local.set(chosen)
            self._show_caution()

    def _save(self) -> None:
        choice = self.choice.get()
        if choice == "unmanaged":
            self.result = ("unmanaged", False)
        elif choice == "local":
            path = self.local.get().strip()
            if not path:
                messagebox.showerror("No folder", "Pick the folder the addon lives in.", parent=self)
                return
            self.result = (f"local:{path}", self.copy.get())
        else:
            repo = self.repo.get().strip().strip("/")
            if repo.count("/") != 1 or not all(repo.split("/")):
                messagebox.showerror("Not a repo", "Write the repo as owner/repo.", parent=self)
                return
            branch = self.branch.get().strip()
            self.result = (f"github:{repo}@{branch}" if self.track.get() and branch else f"github:{repo}", False)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# ── the window ───────────────────────────────────────────────────────────────


class App(ttk.Frame):
    COLUMNS = ("source", "installed", "status")

    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        self.state = core.load()
        self.outbox: queue.Queue = queue.Queue()
        self.worker: _Worker | None = None
        self.failures = self.updated = 0

        master.title("WoW Addons from GitHub")
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
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=200, minwidth=120)
        self.tree.column("source", width=280, minwidth=140)
        self.tree.column("installed", width=100, minwidth=70, anchor="w")
        self.tree.column("status", width=180, minwidth=100)
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
        self.update_button = ttk.Button(buttons, text="Update selected", command=self.update_selected)
        self.update_button.grid(row=0, column=3, padx=4)
        self.update_all_button = ttk.Button(buttons, text="Update all", command=self.update_all)
        self.update_all_button.grid(row=0, column=4, padx=4)
        self.cancel_button = ttk.Button(buttons, text="Stop", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=5, padx=4)

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
        if self.guard(lambda: core.set_source(self.state, name, source, copy=copy)) is None:
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

    def start(self, names: list[str]) -> None:
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
        self.progress.configure(maximum=len(names), value=0)
        self.counter.configure(text=f"0/{len(names)}")
        self.say("Working…")

        self.worker = _Worker(names, self.entries(), root, self.outbox)
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
        if self.tree.exists(result.name):
            self.tree.set(result.name, "source", core.tilde(entry.get("source", "unmanaged")))
            self.tree.set(result.name, "installed", entry.get("installed") or "")
            if result.failed:
                # Per-row failures stay on their row.
                self.tree.set(result.name, "status", result.detail.splitlines()[0])
                self.tree.item(result.name, tags=["failed"])
            elif result.outcome == core.UP_TO_DATE:
                self.tree.set(result.name, "status", "up to date")
                self.tree.item(result.name, tags=[])
            else:
                note = "; ".join(m for _l, m in result.notes)
                self.tree.set(result.name, "status", note or "updated")
                self.tree.item(result.name, tags=[])
        if result.failed:
            self.failures += 1
        elif result.outcome == core.CHANGED:
            self.updated += 1

    def _finished(self) -> None:
        core.save(self.state)
        self.worker = None
        self.counter.configure(text="")
        self.progress.configure(value=0)
        tail = f", {self.failures} failed" if self.failures else ""
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
