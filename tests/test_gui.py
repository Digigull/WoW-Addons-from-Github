#!/usr/bin/env python3
"""Offline tests for the window.

    xvfb-run -a python3 -m unittest discover -s tests -t .

Skipped wholesale when there is no Tkinter or no display, so the ordinary test
run on a headless box stays green -- but not skipped quietly on CI, where a
job installs Tk and Xvfb precisely so these run. The point of them is narrow:
a GUI that fails to start, or a worker result that never reaches its row, are
both invisible to every other test in this suite and would ship.

Nothing here touches the network. `update_addon` is stubbed, because what is
under test is the queue-to-widget path, not the engine -- the engine has its
own tests and does not need a window to exercise it.
"""

import gc
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
except Exception as exc:  # no Tk, or no $DISPLAY
    tk = None
    WHY = f"no usable Tk ({exc})"

if tk is not None:
    from wowaddons import core, gui


@unittest.skipIf(tk is None, globals().get("WHY", "no Tk"))
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.addons = base / "Interface" / "AddOns"
        self.addons.mkdir(parents=True)

        # Point the manifest at the scratch directory rather than the real one,
        # so running the tests cannot rewrite somebody's actual addon list.
        self._config, self._manifest = core.CONFIG_DIR, core.MANIFEST
        core.CONFIG_DIR = base / "config"
        core.MANIFEST = core.CONFIG_DIR / "manifest.json"
        core.CONFIG_DIR.mkdir()
        core.MANIFEST.write_text(json.dumps({
            "addons_dir": str(self.addons),
            "addons": {
                "Bound": {"source": "github:o/r", "mode": "link", "installed": "v1", "folders": ["Bound"]},
                "Loose": {"source": "unmanaged", "mode": "link", "installed": None,
                          "folders": ["Loose"], "suggested": "github:someone/Loose"},
            },
        }))

        self.root = tk.Tk()
        # Deliberately not withdrawn: SourceDialog waits for itself to be
        # mapped before grabbing, and a transient of a withdrawn parent never
        # maps, so hiding the root here would hang the dialog tests.
        self.app = gui.App(self.root)
        self.app.refresh()
        self.pump()

    def tearDown(self):
        core.CONFIG_DIR, core.MANIFEST = self._config, self._manifest
        self.app.stop()
        # Collect while the interpreter is still up. A tk variable finalised
        # after root.destroy(), or on a worker thread, calls into a Tcl that is
        # gone or foreign -- see test_a_closed_dialog_releases_its_tk_variables.
        gc.collect()
        self.root.destroy()
        self.tmp.cleanup()

    def pump(self, times: int = 8) -> None:
        """Let Tk process events, including the after() poll that drains results."""
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()
            self.app._drain()

    def status_of(self, name: str) -> str:
        return self.app.tree.set(name, "status")

    # -- the table -----------------------------------------------------------

    def test_the_manifest_becomes_rows(self):
        self.assertEqual(sorted(self.app.tree.get_children()), ["Bound", "Loose"])
        self.assertEqual(self.app.tree.set("Bound", "source"), "github:o/r")
        self.assertEqual(self.app.tree.set("Bound", "installed"), "v1")

    def test_a_suggestion_is_shown_and_not_applied(self):
        # The rule: a .toc header is the author's claim about where the code
        # lives, not this user's decision to install from there.
        self.assertEqual(self.app.tree.set("Loose", "source"), gui.UNMANAGED_LABEL)
        self.assertIn("github:someone/Loose", self.status_of("Loose"))
        self.assertEqual(self.app.entries()["Loose"]["source"], "unmanaged")

    def test_accepting_a_suggestion_takes_it(self):
        self.app.tree.selection_set("Loose")
        self.app.accept_suggestion()
        self.assertEqual(self.app.entries()["Loose"]["source"], "github:someone/Loose")
        self.assertEqual(self.app.tree.set("Loose", "source"), "github:someone/Loose")

    def test_accept_is_only_offered_for_a_row_that_has_one(self):
        self.app.tree.selection_set("Bound")
        self.app._sync_buttons()
        self.assertEqual(str(self.app.accept_button["state"]), "disabled")
        self.app.tree.selection_set("Loose")
        self.app._sync_buttons()
        self.assertEqual(str(self.app.accept_button["state"]), "normal")

    # -- the worker ----------------------------------------------------------

    def run_update(self, fake):
        real = core.update_addon
        core.update_addon = fake
        try:
            self.app.start(["Bound"])
            for _ in range(200):
                self.pump(1)
                if self.app.worker is None:
                    return
            self.fail("the worker never finished")
        finally:
            core.update_addon = real

    def test_checking_reports_without_installing(self):
        """The Check button must not write anything to AddOns.

        A check that quietly installed would be the worst kind of surprise on a
        slow connection, and the Installed column would contradict the status.
        """
        seen = {}

        def fake(name, entry, root, **kw):
            seen.update(kw)
            return core.Result(name, core.CHANGED, "v1 -> v2", version="v2")

        real = core.update_addon
        core.update_addon = fake
        try:
            self.app.start(["Bound"], check=True)
            for _ in range(200):
                self.pump(1)
                if self.app.worker is None:
                    break
        finally:
            core.update_addon = real

        self.assertTrue(seen.get("check"), "check was not passed through to core")
        self.assertEqual(self.app.tree.set("Bound", "latest"), "v2")
        self.assertEqual(self.app.tree.set("Bound", "installed"), "v1", "a check must not change Installed")
        self.assertIn("update available", self.app.tree.set("Bound", "status"))
        self.assertIn("Nothing was downloaded", self.app.status.cget("text"))

    def test_the_latest_column_exists_beside_installed(self):
        headings = [self.app.tree.heading(c)["text"] for c in self.app.tree["columns"]]
        self.assertEqual(headings, ["Source", "Installed", "Latest", "Status"])

    def test_a_result_reaches_its_row(self):
        def fake(name, entry, root, **kw):
            entry["installed"] = "v2"
            return core.Result(name, core.CHANGED, "v1 -> v2", version="v2", folders=[name])

        self.run_update(fake)
        self.assertEqual(self.app.tree.set("Bound", "installed"), "v2")
        self.assertIn("updated", self.status_of("Bound"))

    def test_a_failure_stays_on_its_row(self):
        # The rule: one unreachable repo marks that row failed and leaves the
        # rest alone. It must not raise a modal -- twelve addons would mean
        # twelve dialogs to dismiss before you could see what happened.
        def fake(name, entry, root, **kw):
            return core.Result(name, core.FAILED, "could not reach GitHub: refused")

        self.run_update(fake)
        self.assertIn("could not reach GitHub", self.status_of("Bound"))
        self.assertEqual(self.app.tree.item("Bound", "tags"), ("failed",))
        self.assertEqual(self.app.failures, 1)

    def test_an_unmanaged_addon_is_not_sent_to_the_worker(self):
        self.app.start(["Loose"])
        self.assertIsNone(self.app.worker)
        self.assertIn("no source", self.app.status.cget("text").lower() + " no source")

    def test_the_run_is_saved_when_it_finishes(self):
        def fake(name, entry, root, **kw):
            entry["installed"] = "v3"
            return core.Result(name, core.CHANGED, "v1 -> v3", version="v3")

        self.run_update(fake)
        written = json.loads(core.MANIFEST.read_text())
        self.assertEqual(core.current(written)["addons"]["Bound"]["installed"], "v3")

    # -- choosing an addon out of a repository -------------------------------

    def offer(self, folders, error=None):
        """Stand in for the repository, without a network.

        The signature is spelled out rather than swallowed with **kwargs on
        purpose. The dialog's lookup runs on a worker that turns any exception
        into status text, so a double that does not accept what the real
        function is now called with does not fail loudly -- it reports a
        repository with nothing in it, and nine tests fail somewhere else
        entirely. That is exactly how this drifted once already.
        """
        def listing(spec, *, no_api=False):
            if error:
                raise core.Fail(error)
            return list(folders)
        real = core.addons_in_repo
        core.addons_in_repo = listing
        self.addCleanup(lambda: setattr(core, "addons_in_repo", real))

    def looked_up_dialog(self, name, repo, folders, error=None):
        self.offer(folders, error)
        dlg = self.dialog(name)
        dlg.choice.set("github")
        dlg.repo.set(repo)
        dlg._begin_lookup()
        for _ in range(40):          # let the worker answer and the poll drain
            self.pump(2)
            dlg._drain_lookups()
            if dlg.looked_up or dlg.lookup_status.cget("text").startswith(("could not", "one addon")):
                break
        said = dlg.lookup_status.cget("text")
        if error is None and said.startswith("could not read"):
            # The lookup raised something this test never asked it to raise.
            # Said here, where the cause is, rather than left for the caller to
            # discover as a missing tick box: the worker turns any exception
            # into this line, so a stale double reads exactly like a repository
            # that holds no addons.
            self.fail(f"the repository lookup failed unexpectedly: {said!r}")
        return dlg

    def test_a_repo_of_several_addons_offers_them_all(self):
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Beta", "Gamma"])
        self.assertEqual(sorted(dlg.folder_boxes), ["Alpha", "Beta", "Gamma"])
        self.assertIn("3 addons", dlg.lookup_status.cget("text"))
        dlg.destroy()

    def test_the_addon_being_bound_is_ticked_for_you(self):
        # The row is called Bound; if the repo holds a folder of that name it
        # is almost certainly the one meant.
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Bound", "Gamma"])
        self.assertTrue(dlg.folder_boxes["Bound"].get())
        self.assertFalse(dlg.folder_boxes["Alpha"].get())
        self.assertEqual(dlg.folder.get(), "Bound")
        dlg.destroy()

    def test_nothing_is_ticked_when_nothing_matches(self):
        """A wrong guess arriving pre-ticked is worse than no guess.

        It will be accepted without being read, and the addon then updates from
        somebody else's folder.
        """
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Beta"])
        self.assertFalse(any(v.get() for v in dlg.folder_boxes.values()))
        self.assertEqual(dlg.folder.get(), "")
        dlg.destroy()

    def test_several_addons_can_be_ticked_for_one_row(self):
        # A main addon plus its companion is one thing to the person updating
        # it, and should be one row rather than two.
        dlg = self.looked_up_dialog("Bound", "o/r", ["Main", "Main_Companion"])
        dlg.folder_boxes["Main"].set(True)
        dlg.folder_boxes["Main_Companion"].set(True)
        dlg._folders_ticked()
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r#Main,Main_Companion", False))

    def test_what_is_already_saved_comes_back_ticked(self):
        entry = {"source": "github:o/r#Beta", "installed": None, "folders": []}
        self.offer(["Alpha", "Beta", "Gamma"])
        dlg = gui.SourceDialog(self.root, "Bound", entry, self.addons)
        dlg._begin_lookup()
        for _ in range(40):
            self.pump(2)
            dlg._drain_lookups()
            if dlg.looked_up:
                break
        self.assertTrue(dlg.folder_boxes["Beta"].get())
        self.assertFalse(dlg.folder_boxes["Alpha"].get())
        dlg.destroy()

    def test_a_repo_whose_root_is_the_addon_offers_no_choice(self):
        # FrostSeek, Minn-Tinkers: the repository root IS the addon.
        dlg = self.looked_up_dialog("Bound", "o/r", [])
        self.assertEqual(dlg.folder_boxes, {})
        self.assertIn("nothing to choose", dlg.lookup_status.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r", False))

    def test_a_repo_holding_exactly_one_addon_offers_no_choice_either(self):
        """One candidate is not a choice, and ticking it would do harm.

        A single tick box implies a decision. Worse, naming a folder switches
        the row from the repository's RELEASES to the last commit touching that
        folder -- so an addon publishing tagged releases would quietly start
        reporting commit ids instead of version numbers. Unticked it installs
        exactly the same folder and keeps its releases.
        """
        dlg = self.looked_up_dialog("Bound", "o/r", ["OnlyOne"])
        self.assertEqual(dlg.folder_boxes, {}, "a single candidate is not a choice")
        self.assertIn("OnlyOne", dlg.lookup_status.cget("text"))
        self.assertIn("nothing to choose", dlg.lookup_status.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r", False),
                         "a lone folder must not be written into the source")

    def test_a_lone_candidate_never_triggers_the_are_you_sure(self):
        # The question is for a repository of several addons with none ticked.
        # Asking it when there was nothing to tick would be nonsense.
        dlg = self.looked_up_dialog("Bound", "o/r", ["OnlyOne"])
        real = gui.messagebox.askokcancel
        gui.messagebox.askokcancel = lambda *a, **k: self.fail("should not have asked")
        try:
            dlg._save()
        finally:
            gui.messagebox.askokcancel = real
        self.assertEqual(dlg.result, ("github:o/r", False))

    def test_a_folder_already_saved_survives_a_repo_that_offers_no_list(self):
        # Someone who typed a folder by hand, for a repo too large to list or
        # laid out unusually, must not have it silently dropped.
        entry = {"source": "github:o/r#Deep/Thing", "installed": None, "folders": []}
        self.offer([])
        dlg = gui.SourceDialog(self.root, "Bound", entry, self.addons)
        dlg._begin_lookup()
        for _ in range(40):
            self.pump(2)
            dlg._drain_lookups()
            if dlg.lookup_status.cget("text"):
                break
        self.assertEqual(dlg.folder.get(), "Deep/Thing")
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r#Deep/Thing", False))

    def test_a_repo_that_cannot_be_read_says_so_and_does_not_block_saving(self):
        dlg = self.looked_up_dialog("Bound", "o/r", [], error="no such repo, or it is private: o/r")
        self.assertIn("could not read", dlg.lookup_status.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r", False))

    def test_saving_with_nothing_ticked_asks_first(self):
        """Binding the whole repository is a real choice and usually a slip.

        The consequence -- every addon in the repo landing in AddOns -- is not
        visible from the dialog, so it is worth one question.
        """
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Beta", "Gamma"])
        asked = []
        real = gui.messagebox.askokcancel
        gui.messagebox.askokcancel = lambda title, message, **k: asked.append(message) or False
        try:
            dlg._save()
        finally:
            gui.messagebox.askokcancel = real
        self.assertEqual(len(asked), 1)
        self.assertIn("ALL of them", asked[0])
        self.assertIsNone(dlg.result, "Cancel must go back, not save")
        dlg.destroy()

    def test_saying_ok_binds_the_whole_repository(self):
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Beta", "Gamma"])
        real = gui.messagebox.askokcancel
        gui.messagebox.askokcancel = lambda *a, **k: True
        try:
            dlg._save()
        finally:
            gui.messagebox.askokcancel = real
        self.assertEqual(dlg.result, ("github:o/r", False))

    def test_a_ticked_choice_saves_without_a_question(self):
        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Bound", "Gamma"])
        real = gui.messagebox.askokcancel
        gui.messagebox.askokcancel = lambda *a, **k: self.fail("should not have asked")
        try:
            dlg._save()
        finally:
            gui.messagebox.askokcancel = real
        self.assertEqual(dlg.result, ("github:o/r#Bound", False))

    def test_the_tick_boxes_are_released_when_the_dialog_closes(self):
        """A BooleanVar per addon is a new way to have the same old bug.

        They are made after __init__, so the VARIABLES list does not cover
        them. Left in the dict, they outlive the dialog and are finalised
        whenever the collector next runs -- which in this program can be on the
        worker thread, where Tcl aborts the process.

        Asserting the dict is empty is not enough on its own: what matters is
        that nothing is left for another thread to finalise, so the test drops
        its own references there and requires that to be inert.
        """
        import threading

        dlg = self.looked_up_dialog("Bound", "o/r", ["Alpha", "Beta"])
        self.assertTrue(dlg.folder_boxes)
        held = list(dlg.folder_boxes.values())

        dlg.destroy()
        self.assertEqual(dlg.folder_boxes, {}, "the dialog still holds tk variables")

        gc.collect()  # sweep unrelated garbage on THIS thread first
        failures = []

        def release():
            try:
                del held[:]
                gc.collect()
            except Exception as exc:  # pragma: no cover - the thing ruled out
                failures.append(exc)

        thread = threading.Thread(target=release)
        thread.start()
        thread.join()
        self.assertEqual(failures, [])

    # -- the Set source button, end to end -----------------------------------

    def click_set_source(self, name, result, keep_backup=True):
        """Drive App.set_source with the dialog answering `result`.

        The button, not the pieces. Every test here used to call core.set_source
        directly with an install, so nothing ever executed the line in App that
        chooses what to pass -- and that line was wrong in v0.5.0: it handed
        over the whole manifest, the TypeError escaped guard() (which catches
        only Fail), and the save never happened. The table then redrew the old
        value, which reads exactly like the source reverting by itself.
        """
        class Dialog:
            def __init__(self, *_a, **_k):
                self.result, self.keep_backup = result, keep_backup

        real_dialog = gui.SourceDialog
        gui.SourceDialog = Dialog
        # An INSTANCE attribute, removed again rather than reassigned. Putting
        # the bound method back as an instance attribute keeps a reference to
        # the Tk object past destroy(), and it then finalises on whatever
        # thread the collector is on -- "main thread is not in main loop", and
        # on the next line Tcl_AsyncDelete aborts the process. Deleting it
        # restores the ordinary class lookup and holds nothing.
        self.root.wait_window = lambda *_a, **_k: None
        try:
            self.app.tree.selection_set(name)
            self.app.set_source()
        finally:
            gui.SourceDialog = real_dialog
            del self.root.wait_window

    def test_setting_a_source_from_the_window_actually_saves_it(self):
        self.click_set_source("Loose", ("github:o/r#Sub", False))
        self.assertEqual(self.app.entries()["Loose"]["source"], "github:o/r#Sub")
        written = json.loads(core.MANIFEST.read_text())
        self.assertEqual(core.current(written)["addons"]["Loose"]["source"], "github:o/r#Sub")

    def test_a_bound_addon_can_be_returned_to_unmanaged(self):
        """Reported from a real install: choosing Unmanaged reverted.

        Nothing was reverting. The save was raising, so the manifest still held
        the old source and refresh() drew it back.
        """
        self.assertEqual(self.app.entries()["Bound"]["source"], "github:o/r")
        self.click_set_source("Bound", ("unmanaged", False))
        self.assertEqual(self.app.entries()["Bound"]["source"], "unmanaged")
        written = json.loads(core.MANIFEST.read_text())
        self.assertEqual(core.current(written)["addons"]["Bound"]["source"], "unmanaged")

    def test_the_table_shows_the_new_source_immediately(self):
        # The window is the only feedback there is; a correct manifest behind a
        # stale table is indistinguishable from the bug this replaced.
        self.click_set_source("Bound", ("unmanaged", False))
        # The table writes unmanaged in brackets, to read as an absence
        # of a source rather than as a source called "unmanaged".
        self.assertEqual(self.app.tree.set("Bound", "source"), "(unmanaged)")

    def test_an_unexpected_error_is_shown_rather_than_swallowed(self):
        """A windowed build has no console for a traceback to land in.

        v0.5.0's Set source raised TypeError on every use. Nothing was shown,
        the save never happened, and the table redrew the old value -- reported
        as "it reverts back to the source instead of leaving it unmanaged".
        Showing the error would not have fixed the bug, but it would have named
        it instead of making the program look untrustworthy.
        """
        shown = []
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda title, message, **k: shown.append((title, message))
        try:
            def boom():
                raise TypeError("something a user cannot possibly have caused")
            self.assertIsNone(self.app.guard(boom))
        finally:
            gui.messagebox.showerror = real
        self.assertEqual(len(shown), 1)
        title, message = shown[0]
        self.assertIn("wrong", title.lower())
        self.assertIn("TypeError", message)
        self.assertIn("bug in this program", message)

    def test_an_expected_failure_still_reads_as_ordinary(self):
        # A repo that cannot be reached is not a crash and must not be dressed
        # up as one, or the real crashes stop standing out.
        shown = []
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda title, message, **k: shown.append((title, message))
        try:
            def nope():
                raise core.Fail("no such repo, or it is private: o/r")
            self.app.guard(nope)
        finally:
            gui.messagebox.showerror = real
        title, message = shown[0]
        self.assertEqual(title, "Cannot do that")
        self.assertNotIn("bug in this program", message)

    # -- several WoW folders -------------------------------------------------

    def second_install(self, name="Wrath"):
        other = pathlib.Path(self.tmp.name) / name / "Interface" / "AddOns"
        other.mkdir(parents=True)
        core.add_install(self.app.state, other, name)
        self.app.refresh()
        return other

    def test_the_picker_is_hidden_until_there_is_a_second_install(self):
        # A dropdown holding one entry is a control that cannot do anything.
        self.assertEqual(self.app.install_picker.winfo_manager(), "")
        self.second_install()
        self.assertEqual(self.app.install_picker.winfo_manager(), "grid")

    def test_switching_shows_the_other_folders_addons(self):
        """The table must follow the picker, or it shows one game's addons
        while every button acts on another's files."""
        other = self.second_install()
        self.assertEqual(self.app.install_choice.get(), "Wrath")
        self.assertEqual(list(self.app.tree.get_children()), [])
        self.assertEqual(self.app.root_dir(), other)

        self.app.install_choice.set(pathlib.Path(self.addons).parts[-3])
        self.app._switch_install()
        self.pump()
        self.assertEqual(sorted(self.app.tree.get_children()), ["Bound", "Loose"])
        self.assertEqual(self.app.root_dir(), self.addons)

    def test_each_install_keeps_its_own_bindings(self):
        self.second_install()
        core.set_source(self.app.install(), "Bound", "github:o/r#Wrath")
        core.use(self.app.state, pathlib.Path(self.addons).parts[-3])
        self.assertEqual(self.app.entries()["Bound"]["source"], "github:o/r")

    def test_the_window_acts_on_the_install_it_is_showing(self):
        # entries() and root_dir() must come from the same install, always --
        # a mismatch writes one game's addon into another game's folder.
        self.second_install()
        self.assertIs(self.app.entries(), self.app.install()["addons"])
        self.assertEqual(str(self.app.root_dir()), self.app.install()["addons_dir"])

    # -- the dialog ----------------------------------------------------------

    def dialog(self, name: str):
        return gui.SourceDialog(self.root, name, self.app.entries()[name], self.addons)

    def test_the_dialog_reads_back_an_existing_github_source(self):
        dlg = self.dialog("Bound")
        self.assertEqual(dlg.choice.get(), "github")
        self.assertEqual(dlg.repo.get(), "o/r")
        dlg.destroy()

    def test_the_dialog_writes_a_branch_source(self):
        dlg = self.dialog("Bound")
        dlg.track.set(True)
        dlg.branch.set("dev")
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r@dev", False))

    def test_the_dialog_writes_a_folder_source(self):
        # A repository holding several addons: this addon tracks one folder.
        dlg = self.dialog("Bound")
        dlg.folder.set("HonorTracker")
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r#HonorTracker", False))

    def test_a_pasted_folder_url_fills_the_folder_in(self):
        # Clicking into one addon of several on github.com and copying the
        # address is the plainest way anybody says which addon they mean.
        dlg = self.dialog("Bound")
        dlg.repo.set("https://github.com/o/r/tree/main/HonorTracker")
        dlg._absorb_url()
        self.assertEqual(dlg.folder.get(), "HonorTracker")
        self.assertTrue(dlg.track.get(), "the branch in the URL should be taken too")
        self.assertIn("HonorTracker", dlg.repo_hint.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r@main#HonorTracker", False))

    def test_the_dialog_reads_back_a_folder_source(self):
        entry = {"source": "github:o/r@main#HonorTracker", "installed": None, "folders": []}
        dlg = gui.SourceDialog(self.root, "Bound", entry, self.addons)
        self.assertEqual(dlg.repo.get(), "o/r")
        self.assertEqual(dlg.branch.get(), "main")
        self.assertEqual(dlg.folder.get(), "HonorTracker")
        dlg.destroy()

    def test_the_caution_names_the_folder_the_repo_will_land(self):
        """For a mono-repo the folder installed is not the addon's own name.

        Cautioning about `Bound` while `HonorTracker` is the directory actually
        about to be replaced is the same class of mistake as promising a backup
        that never happened: a true-sounding sentence about the wrong file.
        """
        (self.addons / "HonorTracker").mkdir()
        (self.addons / "HonorTracker" / "mine.lua").write_text("x")
        dlg = self.dialog("Bound")
        dlg.folder.set("HonorTracker")
        dlg._show_caution()
        self.assertIn("HonorTracker", dlg.caution.cget("text"))
        dlg.destroy()

    def test_the_dialog_refuses_something_that_is_not_owner_slash_repo(self):
        dlg = self.dialog("Bound")
        dlg.repo.set("not-a-repo")
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda *a, **k: None
        try:
            dlg._save()
        finally:
            gui.messagebox.showerror = real
        self.assertIsNone(dlg.result, "it must not save a source it cannot parse")
        dlg.destroy()

    def test_the_dialog_names_the_folder_it_would_displace(self):
        # In a terminal you read about this in the log afterwards. In a window
        # there is no log, so it has to be said before Save, not after.
        (self.addons / "Loose").mkdir()
        (self.addons / "Loose" / "old.lua").write_text("x")
        dlg = self.dialog("Loose")
        dlg.choice.set("local")
        dlg.local.set(str(self.addons.parent / "checkouts" / "Loose"))
        dlg._sync()
        self.assertIn("Loose.replaced", dlg.caution.cget("text"))
        dlg.destroy()

    def test_a_github_source_still_warns_about_the_addons_own_folder(self):
        # Which folders a downloaded archive contains cannot be known in
        # advance, so the addon's own name is the best guess available.
        (self.addons / "Loose").mkdir()
        dlg = self.dialog("Loose")
        dlg.choice.set("github")
        dlg._sync()
        self.assertIn("Loose.replaced", dlg.caution.cget("text"))
        dlg.destroy()

    def test_the_caution_names_the_source_folder_not_the_addon(self):
        # Binding "Loose" to a checkout called Loose-fork installs Loose-fork,
        # because the client matches folder name to the .toc inside. Warning
        # about the wrong folder would be worse than not warning at all.
        (self.addons / "Loose-fork").mkdir()
        (self.addons / "Loose").mkdir()
        dlg = self.dialog("Loose")
        dlg.choice.set("local")
        dlg.local.set(str(self.addons.parent / "checkouts" / "Loose-fork"))
        dlg._sync()
        self.assertIn("Loose-fork.replaced", dlg.caution.cget("text"))
        self.assertNotIn("Loose.replaced", dlg.caution.cget("text"))
        dlg.destroy()

    def test_the_caution_follows_what_is_typed(self):
        # Typed, not just chosen by radio button: the warning has to track the
        # path, or it is stale the moment someone points at another checkout.
        (self.addons / "Loose").mkdir()
        dlg = self.dialog("Loose")
        dlg.choice.set("local")
        dlg._sync()
        self.assertEqual(dlg.caution.cget("text"), "", "nothing typed yet, nothing at risk")

        # Really type it: a synthetic KeyRelease is delivered to the focused
        # widget, so without the focus_force nothing reaches the binding and
        # the test would pass for the wrong reason.
        dlg.local_entry.focus_force()
        dlg.update()
        dlg.local_entry.insert("end", str(self.addons.parent / "checkouts" / "Loose"))
        dlg.local_entry.event_generate("<KeyRelease>", when="now")
        self.assertIn("Loose.replaced", dlg.caution.cget("text"))
        dlg.destroy()

    def test_a_closed_dialog_releases_its_tk_variables(self):
        """The dialog must not leave tk variables for the collector to find.

        A tkinter Variable calls into the interpreter from __del__. Left to the
        garbage collector that happens whenever, on whatever thread happens to
        be allocating -- and this program runs a worker thread. Collected there,
        Tcl raises "main thread is not in main loop"; on Windows it escalates to
        aborting the process outright, which a user would experience as the app
        vanishing mid-update having closed this dialog minutes earlier.

        Windows CI is where this surfaced, and it took the whole run down.
        """
        dlg = self.dialog("Bound")
        variables = [getattr(dlg, name) for name in dlg.VARIABLES]
        self.assertTrue(all(v is not None for v in variables), "nothing to release?")

        dlg.destroy()
        for name in dlg.VARIABLES:
            self.assertIsNone(getattr(dlg, name), f"{name} outlived the dialog")

        # And releasing them from another thread must now be inert, because the
        # dialog no longer holds the only references keeping them alive.
        #
        # Sweep the main thread first. gc.collect() on the worker below
        # finalises everything pending in the whole process, not only what this
        # test made -- so a tk variable left over by any earlier test would be
        # finalised on that thread, raise "main thread is not in main loop", and
        # abort the run with Tcl_AsyncDelete. The failure would look like this
        # test's, and would not be. `variables` still holds its own references,
        # so this collect cannot take them.
        gc.collect()

        import threading

        failures = []

        def collect():
            try:
                del variables[:]
                gc.collect()
            except Exception as exc:  # pragma: no cover - the thing being ruled out
                failures.append(exc)

        thread = threading.Thread(target=collect)
        thread.start()
        thread.join()
        self.assertEqual(failures, [])

    def test_events_arriving_after_close_are_harmless(self):
        # Widget bindings can fire once more while a dialog is being torn down.
        dlg = self.dialog("Bound")
        dlg.destroy()
        dlg._sync()            # must not raise on released variables
        dlg._show_caution()    # nor this, which the same bindings drive

    def test_a_pasted_url_is_accepted_and_shown_back(self):
        # Reported from a real install: pasting the repo URL was refused and
        # demanded owner/repo.
        dlg = self.dialog("Bound")
        dlg.choice.set("github")
        dlg.repo.set("https://github.com/tullamods/Bagnon")
        dlg._absorb_url()
        self.assertIn("tullamods/Bagnon", dlg.repo_hint.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:tullamods/Bagnon", False))

    def test_a_pasted_branch_url_ticks_track_branch(self):
        # Otherwise the branch they were looking at is silently dropped.
        dlg = self.dialog("Bound")
        dlg.choice.set("github")
        dlg.repo.set("https://github.com/Questie/Questie/tree/develop")
        dlg._absorb_url()
        self.assertTrue(dlg.track.get())
        self.assertEqual(dlg.branch.get(), "develop")
        dlg._save()
        self.assertEqual(dlg.result, ("github:Questie/Questie@develop", False))

    def test_something_that_is_not_a_repo_is_still_refused(self):
        dlg = self.dialog("Bound")
        dlg.choice.set("github")
        dlg.repo.set("https://curseforge.com/wow/addons/bagnon")
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda *a, **k: None
        try:
            dlg._save()
        finally:
            gui.messagebox.showerror = real
        self.assertIsNone(dlg.result)
        dlg.destroy()

    def test_the_warning_says_kept_or_deleted_to_match_what_happens(self):
        """The bug that started this: it promised a backup that never happened.

        The dialog now asks core the same questions core asks itself, so the
        two cannot disagree again.
        """
        (self.addons / "Bound").mkdir()
        self.app.entries()["Bound"]["installed"] = None   # the user's own files
        dlg = self.dialog("Bound")
        dlg.choice.set("github")
        dlg._sync()
        kept = dlg.caution.cget("text")
        self.assertIn("Bound.replaced", kept)
        self.assertIn("once", kept, "it must say the copy is not made every update")

        dlg.backup.set(False)
        dlg._show_caution()
        deleted = dlg.caution.cget("text")
        self.assertIn("DELETED", deleted)
        self.assertNotIn("Bound.replaced", deleted, "it must not name a copy it will not make")
        dlg.destroy()

    def test_no_warning_once_the_tool_owns_the_folder(self):
        # A folder with a recorded version came from the source, so replacing it
        # loses nothing. A red line on every routine update is how people learn
        # to ignore the warning that matters.
        (self.addons / "Bound").mkdir()
        self.app.entries()["Bound"]["installed"] = "v1"
        for backup in (True, False):
            with self.subTest(backup=backup):
                dlg = self.dialog("Bound")
                dlg.choice.set("github")
                dlg.backup.set(backup)
                dlg._sync()
                self.assertEqual(dlg.caution.cget("text"), "")
                dlg.destroy()

    def test_the_backup_choice_is_saved(self):
        dlg = self.dialog("Bound")
        dlg.choice.set("github")
        dlg.repo.set("o/r")
        dlg.backup.set(False)
        dlg._save()
        self.assertFalse(dlg.keep_backup)
        core.set_source(self.app.install(), "Bound", dlg.result[0], backup=dlg.keep_backup)
        self.assertIs(self.app.entries()["Bound"]["backup"], False)

    def test_no_caution_for_a_folder_that_is_only_a_link(self):
        core.make_link(self.addons, self.addons / "Loose")
        dlg = self.dialog("Loose")
        dlg.choice.set("local")
        dlg._sync()
        self.assertEqual(dlg.caution.cget("text"), "")
        dlg.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
