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
import threading
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


def setUpModule():
    """No test in this file may look for a real GitHub token.

    Every window resolves the token source when it opens, to draw the GitHub
    label -- so without this, each of a hundred tests reaches the system
    keyring and `git credential fill` on a worker thread. On Windows CI that is
    a PowerShell process apiece, and on a developer's own machine it can find
    their live token, which the suite would then be free to send somewhere.

    Pinned to "nothing saved". The tests that are *about* the token restore the
    real lookups and stub one layer lower, at the keyring itself.
    """
    if tk is None:
        return
    global _real_stored_token, _real_credential_token
    _real_stored_token = core.stored_token
    _real_credential_token = core.credential_token
    core.stored_token = lambda: None
    core.credential_token = lambda: None
    core.forget_cached_token()
    os.environ.pop("GITHUB_TOKEN", None)


def tearDownModule():
    if tk is None:
        return
    core.stored_token = _real_stored_token
    core.credential_token = _real_credential_token
    core.forget_cached_token()


@unittest.skipIf(tk is None, globals().get("WHY", "no Tk"))
class WindowHarness(unittest.TestCase):
    """A real window over a scratch AddOns folder. No network, no real manifest.

    Shared rather than copied per test class: the setup that matters is the
    redirection of CONFIG_DIR, and a copy of it that drifts would write into
    somebody's actual manifest while the tests looked green.
    """

    ADDONS: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.addons = base / "Interface" / "AddOns"
        self.addons.mkdir(parents=True)
        # Dialogs to close before the root goes. addCleanup runs *after*
        # tearDown, by which point there is no interpreter left to destroy a
        # window with -- and a dialog torn down with the root cannot release
        # its tk variables on this thread, which is the whole point of its
        # destroy().
        self.opened = []

        # Point the manifest at the scratch directory rather than the real one,
        # so running the tests cannot rewrite somebody's actual addon list.
        self._config, self._manifest = core.CONFIG_DIR, core.MANIFEST
        core.CONFIG_DIR = base / "config"
        core.MANIFEST = core.CONFIG_DIR / "manifest.json"
        core.CONFIG_DIR.mkdir()
        core.MANIFEST.write_text(json.dumps({
            "addons_dir": str(self.addons),
            "addons": dict(self.ADDONS),
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
        for dialog in self.opened:
            dialog.destroy()
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


class WindowTests(WindowHarness):
    ADDONS = {
        "Bound": {"source": "github:o/r", "mode": "link", "installed": "v1", "folders": ["Bound"]},
        "Loose": {"source": "unmanaged", "mode": "link", "installed": None,
                  "folders": ["Loose"], "suggested": "github:someone/Loose"},
    }

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
        self.assertEqual(self.status_of("Bound"), "recently updated")

    def test_what_happened_on_the_way_is_not_left_on_the_row(self):
        """The Status column is scanned for what changed, not for how.

        The engine narrates each step it takes -- a folder moved aside,
        settings deleted -- and every one of those was agreed to a moment
        earlier in the confirm dialog. In a 170-pixel column it crowded out the
        only thing this column is read for.
        """
        def fake(name, entry, root, **kw):
            entry["installed"] = "v2"
            result = core.Result(name, core.CHANGED, "v1 -> v2", version="v2", folders=[name])
            result.notes.append(("note", "moving existing folder aside -> Bound.replaced"))
            return result

        self.run_update(fake)
        self.assertEqual(self.status_of("Bound"), "recently updated")
        self.assertEqual(self.app.tree.item("Bound", "tags"), "")

    def test_a_warning_inside_a_successful_run_does_stay(self):
        # Not narration: something that did not go to plan. Silence about it
        # would be the row claiming a clean run it did not have.
        def fake(name, entry, root, **kw):
            entry["installed"] = "v2"
            result = core.Result(name, core.CHANGED, "v1 -> v2", version="v2", folders=[name])
            result.notes.append(("warn", "saved variables: Bound.lua: Permission denied"))
            return result

        self.run_update(fake)
        self.assertIn("Permission denied", self.status_of("Bound"))
        self.assertEqual(self.app.tree.item("Bound", "tags"), ("suggested",))

    def test_a_row_that_installs_a_whole_repository_still_says_so(self):
        # The one note worth the column: this row now rewrites every addon in
        # the repository whenever any one of them changes.
        def fake(name, entry, root, **kw):
            entry["installed"] = "v2"
            entry["folders"] = ["Bound", "Bound_Extra", "Bound_Third"]
            return core.Result(name, core.CHANGED, "v1 -> v2", version="v2",
                               folders=entry["folders"])

        self.run_update(fake)
        self.assertEqual(self.status_of("Bound"), "installs 3 addons")

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

    def test_a_toc_per_client_repo_offers_the_tocs_and_says_why(self):
        """RichSteini/NotPlater: one addon, two .toc files, no base between them.

        The folder in AddOns has to be named after the one you want, so this is
        a choice — and one the tool cannot make, because it is which client you
        play. Saying "2 addons" would be a lie about what is on offer.
        """
        dlg = self.looked_up_dialog("Bound", "o/r",
                                    ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        self.assertEqual(sorted(dlg.folder_boxes),
                         ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        self.assertIn(".toc files", dlg.lookup_status.cget("text"))
        dlg.destroy()

    def test_saving_a_toc_choice_with_nothing_ticked_is_refused_outright(self):
        # Not the "OK installs ALL of them" question: every .toc here names the
        # same files, so all of them would install one addon twice over under
        # names only one of which the client loads. There is nothing to say OK to.
        dlg = self.looked_up_dialog("Bound", "o/r",
                                    ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        shown = []
        real_error, real_ask = gui.messagebox.showerror, gui.messagebox.askokcancel
        gui.messagebox.showerror = lambda title, message, **k: shown.append(title)
        gui.messagebox.askokcancel = lambda *a, **k: self.fail("this is not an OK/Cancel question")
        try:
            dlg._save()
        finally:
            gui.messagebox.showerror, gui.messagebox.askokcancel = real_error, real_ask
        self.assertEqual(shown, ["Which client?"])
        self.assertIsNone(dlg.result)
        dlg.destroy()

    def test_ticking_one_toc_binds_that_build(self):
        dlg = self.looked_up_dialog("Bound", "o/r",
                                    ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        dlg.folder_boxes["NotPlater-3.3.5.toc"].set(True)
        dlg._folders_ticked()
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r#NotPlater-3.3.5.toc", False))

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
        dlg = self.looked_up_dialog("Bound", "o/r", [], error="tripped over the cat")
        self.assertIn("could not read", dlg.lookup_status.cget("text"))
        dlg._save()
        self.assertEqual(dlg.result, ("github:o/r", False))

    def test_a_message_that_already_names_the_repo_is_not_prefixed_with_it_again(self):
        """The private-repo message is the one people actually have to read.

        It names the repository and then says what to do about it. Wrapping
        that in "could not read o/r: ..." says the name twice and pushes the
        actionable half further from the eye, in the one dialog where somebody
        is already stuck.
        """
        message = ("cannot see o/r. Either it does not exist, or it is private -- "
                   "private repositories need a GitHub token (Sign in... in the window, "
                   "or set GITHUB_TOKEN).")
        dlg = self.looked_up_dialog("Bound", "o/r", [], error=message)
        self.assertEqual(dlg.lookup_status.cget("text"), message)
        self.assertEqual(dlg.lookup_status.cget("text").count("o/r"), 1)

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


class RescanSaysWhatItSkipped(WindowHarness):
    """A folder you can see in AddOns and cannot see in the list needs a reason.

    Reported against 0.9.0 as "it is not finding PlayerbotManager": the folder
    was there, the scan dropped it, and the window said nothing about it at all.
    """

    def rescan_reporting(self) -> list:
        shown = []
        real = gui.messagebox.showinfo
        gui.messagebox.showinfo = lambda title, message, **k: shown.append(message)
        try:
            self.app.rescan()
            self.pump()
        finally:
            gui.messagebox.showinfo = real
        return shown

    def test_a_folder_the_game_cannot_load_is_named_with_its_fix(self):
        broken = self.addons / "PlayerbotManager"
        broken.mkdir()
        (broken / "PlayerbotManager.toc.txt").write_text("## Title: Playerbot Manager\n")

        shown = self.rescan_reporting()
        self.assertTrue(shown, "the skipped folder was never mentioned")
        self.assertIn("PlayerbotManager", shown[0])
        self.assertIn("folder(s) the game cannot load", self.app.status.cget("text"))

    def test_a_loadable_folder_is_listed_and_not_complained_about(self):
        good = self.addons / "PlayerbotManager"
        good.mkdir()
        # The spelling a case-sensitive filesystem keeps and Wine ignores.
        (good / "Playerbotmanager.toc").write_text("## Title: Playerbot Manager\n")

        self.assertEqual(self.rescan_reporting(), [])
        self.assertIn("PlayerbotManager", self.app.tree.get_children())

    def test_the_same_skipped_folder_is_not_reported_twice(self):
        parked = self.addons / "Not Working"
        (parked / "OldOne").mkdir(parents=True)
        (parked / "OldOne" / "OldOne.toc").write_text("## Title: x\n")

        self.assertTrue(self.rescan_reporting())
        self.assertEqual(self.rescan_reporting(), [], "a kept folder must not nag")




class InstallHarness(WindowHarness):
    """Pressing Install with both dialogs and the network stood in for.

    A harness rather than a base class with tests in it: a test class that
    inherits tests re-runs every one of them, which is a slower suite saying
    the same thing twice.
    """

    def offer(self, folders):
        def listing(spec, *, no_api=False):
            return list(folders)
        real = core.addons_in_repo
        core.addons_in_repo = listing
        self.addCleanup(lambda: setattr(core, "addons_in_repo", real))

    def looked_up_dialog(self, repo, folders):
        self.offer(folders)
        dlg = gui.InstallDialog(self.root, self.addons, self.app.entries())
        self.opened.append(dlg)
        dlg.repo.set(repo)
        dlg._absorb_url()          # what typing into the box does
        dlg._begin_lookup()
        for _ in range(40):
            self.pump(2)
            dlg._drain_lookups()
            if dlg.looked_up or "nothing to choose" in dlg.lookup_status.cget("text"):
                break
        said = dlg.lookup_status.cget("text")
        if said.startswith("could not read"):
            self.fail(f"the repository lookup failed unexpectedly: {said!r}")
        return dlg


    def install(self, plan, installs, overwrite="never asked", fails=()):
        """Press Install, with both dialogs and the network stood in for.

        `overwrite` is what the confirm dialog returns when a folder is already
        there; the default fails the test if it is opened at all, so a dialog
        appearing where it should not is never silent.
        """
        asked = []

        class FakeInstall(tk.Toplevel):
            def __init__(inner, parent, root, entries, *, no_api=False):
                super().__init__(parent)
                inner.result = list(plan)
                inner.after(1, inner.destroy)

        class FakeOverwrite(tk.Toplevel):
            def __init__(inner, parent, addon, root, entry):
                super().__init__(parent)
                asked.append(addon)
                if overwrite == "never asked":
                    raise AssertionError(f"asked to confirm overwriting {addon}")
                inner.result = overwrite
                inner.after(1, inner.destroy)

        def update_addon(name, entry, root, **kw):
            if name in fails:
                return core.Result(name=name, outcome=core.FAILED, detail="no such repo")
            folders = installs.get(name, [name])
            entry["installed"] = "v1"
            entry["folders"] = folders
            return core.Result(name=name, outcome=core.CHANGED, version="v1", folders=folders)

        real = (gui.InstallDialog, gui.OverwriteDialog, core.update_addon)
        gui.InstallDialog, gui.OverwriteDialog, core.update_addon = (
            FakeInstall, FakeOverwrite, update_addon)
        try:
            self.app.install_addon()
            for _ in range(200):
                self.pump(1)
                if self.app.worker is None:
                    return asked
            self.fail("the worker never finished")
        finally:
            gui.InstallDialog, gui.OverwriteDialog, core.update_addon = real

class InstallingSomethingNew(InstallHarness):
    """The Install button: an addon that is not in AddOns yet, from a link.

    Everything else in the window works on folders that already exist, which
    left "get me this addon" as the one job you still had to do by hand -- by
    downloading and unzipping an archive correctly, which is the job this tool
    exists to do for you.
    """

    # -- what the dialog decides ---------------------------------------------

    def test_a_repo_holding_one_addon_installs_whole_under_the_folder_name(self):
        # Naming the folder would switch the row off the repo's releases onto
        # that folder's last commit, for an install of the same files.
        dlg = self.looked_up_dialog("o/Bagnon-wotlk", ["Bagnon"])
        self.assertEqual(dlg._plan(), [("Bagnon", "github:o/Bagnon-wotlk")])

    def test_a_repo_that_is_itself_the_addon_is_named_after_the_repo(self):
        dlg = self.looked_up_dialog("o/FrostSeek", [])
        self.assertEqual(dlg._plan(), [("FrostSeek", "github:o/FrostSeek")])

    def test_each_ticked_addon_becomes_its_own_row(self):
        # One row per addon, never one row for the repository: a single row
        # would install all of them whenever any one of them changed.
        dlg = self.looked_up_dialog("o/r", ["Alpha", "Beta", "Gamma"])
        dlg.folder_boxes["Alpha"].set(True)
        dlg.folder_boxes["Gamma"].set(True)
        dlg._folders_ticked()
        self.assertEqual(dlg._plan(), [
            ("Alpha", "github:o/r#Alpha"),
            ("Gamma", "github:o/r#Gamma"),
        ])

    def test_a_toc_per_client_repo_plans_a_row_named_after_the_toc(self):
        dlg = self.looked_up_dialog("o/NotPlater",
                                    ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        dlg.folder_boxes["NotPlater-3.3.5.toc"].set(True)
        dlg._folders_ticked()
        # The row is named after the folder the client will load, and the
        # source keeps the .toc even though only one is ticked: it is not the
        # default install, it is one of two names for the same files.
        self.assertEqual(dlg._plan(),
                         [("NotPlater-3.3.5", "github:o/NotPlater#NotPlater-3.3.5.toc")])

    def test_installing_a_toc_per_client_repo_needs_a_tick(self):
        dlg = self.looked_up_dialog("o/NotPlater",
                                    ["NotPlater-2.4.3.toc", "NotPlater-3.3.5.toc"])
        self.assertIn("Tick the .toc your client uses", dlg.caution.cget("text"))
        shown = []
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda title, message, **k: shown.append(title)
        try:
            dlg._install()
        finally:
            gui.messagebox.showerror = real
        self.assertEqual(shown, ["Which client?"])
        self.assertIsNone(dlg.result)

    def test_a_repo_of_several_with_nothing_ticked_is_refused(self):
        dlg = self.looked_up_dialog("o/r", ["Alpha", "Beta"])
        shown = []
        real = gui.messagebox.showerror
        gui.messagebox.showerror = lambda title, message, **k: shown.append(title)
        try:
            dlg._install()
        finally:
            gui.messagebox.showerror = real
        self.assertEqual(shown, ["Which addon?"])
        self.assertIsNone(dlg.result)
        self.assertTrue(dlg.winfo_exists(), "the dialog must stay open to be answered")

    def test_a_branch_in_the_pasted_link_is_kept(self):
        dlg = self.looked_up_dialog("https://github.com/o/r/tree/wotlk", [])
        self.assertTrue(dlg.track.get())
        self.assertEqual(dlg._plan(), [("r", "github:o/r@wotlk")])

    def test_a_link_into_one_addon_of_several_installs_that_one(self):
        dlg = self.looked_up_dialog("https://github.com/o/r/tree/main/Beta",
                                    ["Alpha", "Beta"])
        self.assertEqual(dlg._plan(), [("Beta", "github:o/r@main#Beta")])

    def test_replacing_a_hand_installed_folder_is_said_in_advance(self):
        (self.addons / "Bagnon").mkdir()
        (self.addons / "Bagnon" / "Bagnon.toc").write_text("## Title: mine\n")
        dlg = self.looked_up_dialog("o/Bagnon", ["Bagnon"])
        self.assertIn("moved aside", dlg.caution.cget("text"))

    # -- and what the window does with it ------------------------------------

    def test_installing_binds_the_row_and_fetches_it(self):
        self.install([("Bagnon", "github:o/Bagnon")], {})
        entry = self.app.entries()["Bagnon"]
        self.assertEqual(entry["source"], "github:o/Bagnon")
        self.assertEqual(entry["installed"], "v1")
        self.assertIn("Bagnon", self.app.tree.get_children())

    def test_the_row_takes_the_name_of_the_folder_that_landed(self):
        """A row named before the archive was open is a guess, not a fact.

        Left wrong, the addon shows up twice: a bound row reading "not
        installed" beside the unmanaged row the next rescan adds for the folder
        that is actually there.
        """
        self.install([("NotPlater-3.3.5", "github:o/NotPlater-3.3.5")],
                     {"NotPlater-3.3.5": ["NotPlater"]})
        self.assertIn("NotPlater", self.app.entries())
        self.assertNotIn("NotPlater-3.3.5", self.app.entries())
        self.assertEqual(self.app.entries()["NotPlater"]["installed"], "v1")
        self.assertIn("NotPlater", self.app.tree.get_children())

    def test_a_row_that_was_already_right_is_left_alone(self):
        self.install([("Bagnon", "github:o/Bagnon")], {"Bagnon": ["Bagnon"]})
        self.assertEqual(list(self.app.entries()), ["Bagnon"])

    def test_the_button_is_off_while_a_run_is_going(self):
        self.app.worker = _StillRunning()
        try:
            self.app._sync_buttons()
            self.assertEqual(str(self.app.install_button["state"]), "disabled")
        finally:
            self.app.worker = None
        self.app._sync_buttons()
        self.assertEqual(str(self.app.install_button["state"]), "normal")


class _StillRunning:
    def is_alive(self):
        return True



class AskingBeforeReplacingWhatIsThere(WindowHarness):
    """The confirm dialog: replace the folder, and optionally wipe the settings.

    The two halves are deliberately unequal. Replacing a folder is undone by
    installing again. Deleting saved variables is undone by nothing -- those are
    settings somebody made over months, and no repository has a copy.
    """

    def wtf(self, addon, *relatives):
        wtf = self.addons.parent.parent / "WTF"
        for relative in relatives:
            path = wtf / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"-- {addon}\n")
        return wtf

    def existing(self, name="Bagnon", ours=False):
        folder = self.addons / name
        folder.mkdir()
        (folder / f"{name}.toc").write_text("## Title: mine\n")
        entry = {"source": f"github:o/{name}", "mode": "link",
                 "installed": "v1" if ours else None,
                 "folders": [name], "backup": True}
        return entry

    def dialog(self, name="Bagnon", ours=False):
        dlg = gui.OverwriteDialog(self.root, name, self.addons, self.existing(name, ours))
        self.opened.append(dlg)
        return dlg

    def test_a_hand_installed_folder_is_offered_a_backup_by_default(self):
        dlg = self.dialog()
        self.assertTrue(dlg.at_risk)
        self.assertTrue(dlg.keep_folder.get())
        self.assertIn("Bagnon.replaced", dlg.keep_box.cget("text"))

    def test_a_folder_this_tool_wrote_is_not_offered_a_pointless_copy(self):
        # Nothing of yours is in there; keeping a copy of a download is not a
        # safeguard, it is litter in the AddOns folder.
        dlg = self.dialog(ours=True)
        self.assertFalse(dlg.at_risk)
        self.assertFalse(dlg.keep_folder.get())
        self.assertFalse(hasattr(dlg, "keep_box"))

    def test_deleting_settings_starts_off_and_its_backup_starts_unavailable(self):
        self.wtf("Bagnon", "Account/ACC/SavedVariables/Bagnon.lua")
        dlg = self.dialog()
        self.assertFalse(dlg.delete_saved.get())
        self.assertEqual(str(dlg.backup_box["state"]), "disabled")
        self.assertTrue(dlg.backup_saved.get(), "the safe answer is the one already chosen")

    def test_ticking_delete_makes_the_backup_choice_available(self):
        self.wtf("Bagnon", "Account/ACC/SavedVariables/Bagnon.lua")
        dlg = self.dialog()
        dlg.delete_saved.set(True)
        dlg._sync()
        self.assertEqual(str(dlg.backup_box["state"]), "normal")

    def test_account_and_character_files_are_both_found_and_named(self):
        self.wtf("Bagnon",
                 "Account/ACC/SavedVariables/Bagnon.lua",
                 "Account/ACC/SavedVariables/Bagnon.lua.bak",
                 "Account/ACC/Frostmourne/Bob/SavedVariables/Bagnon.lua",
                 "Account/ACC/SavedVariables/Someone_Else.lua")
        dlg = self.dialog()
        self.assertEqual(len(dlg.saved), 3)
        shown = "\n".join(dlg._lines())
        self.assertIn("Account/ACC/SavedVariables/Bagnon.lua", shown.replace("\\", "/"))
        self.assertIn("Bob/SavedVariables/Bagnon.lua", shown.replace("\\", "/"))
        self.assertNotIn("Someone_Else", shown)

    def test_nothing_to_delete_disables_the_delete_box_entirely(self):
        dlg = self.dialog()
        self.assertEqual(dlg.saved, [])
        self.assertEqual(str(dlg.delete_box["state"]), "disabled")
        self.assertIn("no saved variables found", " ".join(dlg._lines()))

    def test_the_answer_is_the_three_booleans(self):
        self.wtf("Bagnon", "Account/ACC/SavedVariables/Bagnon.lua")
        dlg = self.dialog()
        dlg.delete_saved.set(True)
        dlg.backup_saved.set(False)
        dlg._go()
        self.assertEqual(dlg.result, {"keep_folder": True, "delete_saved": True,
                                      "backup_saved": False})

    def test_cancel_answers_nothing(self):
        dlg = self.dialog()
        dlg._cancel()
        self.assertIsNone(dlg.result)


class WipingSettingsAsPartOfAnInstall(InstallHarness):
    """Ticking Delete! in the confirm dialog, and what the install then does."""

    def saved(self, addon="Bagnon"):
        wtf = self.addons.parent.parent / "WTF"
        for relative in (f"Account/ACC/SavedVariables/{addon}.lua",
                         f"Account/ACC/Frostmourne/Bob/SavedVariables/{addon}.lua"):
            path = wtf / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("-- settings\n")
        return sorted(p.name for p in wtf.rglob("*") if p.is_file())

    def there_already(self, name="Bagnon"):
        folder = self.addons / name
        folder.mkdir()
        (folder / f"{name}.toc").write_text("## Title: mine\n")

    def files_left(self):
        wtf = self.addons.parent.parent / "WTF"
        return sorted(str(p.relative_to(wtf)).replace("\\", "/")
                      for p in wtf.rglob("*") if p.is_file())

    def test_no_folder_there_means_no_question(self):
        # The default `overwrite` fails the test if the dialog is opened.
        self.assertEqual(self.install([("Bagnon", "github:o/Bagnon")], {}), [])

    def test_a_folder_already_there_is_confirmed_first(self):
        self.there_already()
        asked = self.install([("Bagnon", "github:o/Bagnon")], {},
                             overwrite={"keep_folder": True, "delete_saved": False,
                                        "backup_saved": True})
        self.assertEqual(asked, ["Bagnon"])
        self.assertEqual(self.app.entries()["Bagnon"]["installed"], "v1")

    def test_cancelling_installs_nothing_and_binds_nothing(self):
        self.there_already()
        self.install([("Bagnon", "github:o/Bagnon")], {}, overwrite=None)
        self.assertNotIn("Bagnon", self.app.entries())
        self.assertIn("Nothing installed", self.app.status.cget("text"))

    def test_the_backup_choice_reaches_the_manifest(self):
        self.there_already()
        self.install([("Bagnon", "github:o/Bagnon")], {},
                     overwrite={"keep_folder": False, "delete_saved": False,
                                "backup_saved": True})
        self.assertIs(self.app.entries()["Bagnon"]["backup"], False)

    def test_settings_are_deleted_only_when_asked_and_a_copy_is_kept(self):
        self.there_already()
        self.saved()
        self.install([("Bagnon", "github:o/Bagnon")], {},
                     overwrite={"keep_folder": True, "delete_saved": True,
                                "backup_saved": True})
        self.assertEqual(self.files_left(), [
            "Account/ACC/Frostmourne/Bob/SavedVariables/Bagnon.lua.replaced",
            "Account/ACC/SavedVariables/Bagnon.lua.replaced",
        ])
        self.assertEqual(self.status_of("Bagnon"), "recently updated")
        # Said once, where a run reports itself -- not on the row, which is
        # scanned for what changed, not for how.
        self.assertIn("2 saved variables file(s) deleted", self.app.status.cget("text"))
        self.assertIn("copies kept", self.app.status.cget("text"))

    def test_no_copy_is_kept_when_that_is_what_was_asked(self):
        self.there_already()
        self.saved()
        self.install([("Bagnon", "github:o/Bagnon")], {},
                     overwrite={"keep_folder": True, "delete_saved": True,
                                "backup_saved": False})
        self.assertEqual(self.files_left(), [])

    def test_settings_survive_an_install_that_failed(self):
        """The one ordering that matters.

        Settings are the only thing here no source can fetch again. If the
        download failed the old addon is still installed, and wiping what it
        remembers would be a loss with nothing gained.
        """
        self.there_already()
        self.saved()
        self.install([("Bagnon", "github:o/Bagnon")], {}, fails={"Bagnon"},
                     overwrite={"keep_folder": True, "delete_saved": True,
                                "backup_saved": False})
        self.assertEqual(self.files_left(), [
            "Account/ACC/Frostmourne/Bob/SavedVariables/Bagnon.lua",
            "Account/ACC/SavedVariables/Bagnon.lua",
        ])

    def test_a_delete_is_not_carried_over_to_the_next_run(self):
        self.there_already()
        self.saved()
        self.install([("Bagnon", "github:o/Bagnon")], {}, fails={"Bagnon"},
                     overwrite={"keep_folder": True, "delete_saved": True,
                                "backup_saved": False})
        self.assertEqual(self.app._pending_saved, {})


class SigningInToGitHub(WindowHarness):
    """The window's own answer to "it says my repository does not exist".

    Before this there was nowhere in the window to put a token: it could only
    arrive as an environment variable, which means a terminal, which is not
    where somebody who launched this from the Start Menu is standing.
    """

    def setUp(self):
        super().setUp()
        # A fake keyring, so no test touches a real one. `keyring_works` says
        # whether this machine has somewhere better than a file to offer.
        self.secret = {}
        self.keyring_works = True

        def secret_set(token):
            if not self.keyring_works:
                return False
            self.secret["token"] = token
            return True

        for name, stub in (
            # setUpModule pinned `stored_token` to None for the rest of the
            # file; these tests are about it, so it goes back to the real one
            # and the stubbing happens at the keyring below it.
            ("stored_token", _real_stored_token),
            ("secret_get", lambda: self.secret.get("token")),
            ("secret_set", secret_set),
            ("secret_clear", lambda: self.secret.pop("token", None)),
            ("credential_token", lambda: None),
        ):
            self.addCleanup(setattr, core, name, getattr(core, name))
            setattr(core, name, stub)

        os.environ.pop("GITHUB_TOKEN", None)
        self.addCleanup(lambda: os.environ.pop("GITHUB_TOKEN", None))
        self.addCleanup(core.forget_cached_token)
        core.forget_cached_token()

    def dialog(self):
        dlg = gui.SignInDialog(self.app)
        self.opened.append(dlg)
        self.pump()
        return dlg

    def settled_label(self) -> str:
        """The window's GitHub label, once the background lookup has landed.

        It is drawn from a worker -- asking the keyring and Git is subprocess
        work and must not happen on the thread drawing the window -- so it
        reads "checking…" until the answer arrives.
        """
        self.app._sync_github()
        for _ in range(60):
            self.pump(2)
            self.app._drain_github()
            text = self.app.github_label.cget("text")
            if text != "checking…":
                return text
        self.fail("the GitHub label never settled")

    # -- the label on the main window ---------------------------------------

    def test_the_window_says_when_no_token_is_in_play(self):
        self.assertIn("not signed in", self.settled_label())

    def test_the_window_says_where_the_token_came_from(self):
        """Which source is in use is the thing somebody stuck actually needs.

        Signing in here and still being told the repository does not exist
        means something different depending on whether the token being sent is
        the one just saved or one Git had all along.
        """
        self.secret["token"] = "t"
        core.forget_cached_token()
        self.assertIn("secret store", self.settled_label())

        os.environ["GITHUB_TOKEN"] = "t"
        self.assertIn("GITHUB_TOKEN", self.settled_label())

    # -- the dialog ----------------------------------------------------------

    def test_saving_a_token_signs_you_in(self):
        dlg = self.dialog()
        dlg.token.set("github_pat_x")
        dlg._save()
        self.pump()
        self.assertEqual(core.github_token(), "github_pat_x")
        # Named rather than spelled: the store is a keyring on Linux, a
        # keychain on macOS and DPAPI on Windows, and the label says which.
        self.assertIn(core.secret_store_name(), dlg.state_label.cget("text"))

    def test_the_typed_token_is_cleared_once_it_is_saved(self):
        """A dialog left open should not go on holding it in a widget."""
        dlg = self.dialog()
        dlg.token.set("github_pat_x")
        dlg._save()
        self.pump()
        self.assertEqual(dlg.token.get(), "")

    def test_the_dialog_spells_out_where_the_token_page_is(self):
        """The button cannot help somebody who will not be sent to a browser.

        And the click path is genuinely hard: Developer settings is the last
        item in a sidebar longer than the window, so not scrolling far enough
        is where people give up. Pinned because it is the kind of text that
        gets trimmed for looking wordy, by which point the dialog is back to
        naming a page nobody can find.
        """
        dlg = self.dialog()
        shown = " ".join(
            child.cget("text")
            for frame in dlg.winfo_children()
            for kid in frame.winfo_children()
            for child in ([kid] + list(kid.winfo_children()))
            if "text" in child.keys()
        )
        for step in ("Settings", "Developer settings", "bottom",
                     "Personal access tokens", "Fine-grained tokens"):
            self.assertIn(step, shown, f"the dialog no longer mentions {step!r}")
        self.assertIn("Contents: Read-only", shown)

    def test_closing_the_sign_in_dialog_releases_its_tk_variables(self):
        """Same hazard as the other dialogs, and it needed the same answer.

        Written first with the release in `_close`, which covered the Close
        button and the window's X and nothing else: `destroy()` called
        directly -- how a parent tears its children down, and what the harness
        does -- went through Toplevel's and released nothing. Windows CI
        aborted the whole run with Tcl_AsyncDelete, exactly as the older test
        beside this one warned it would.
        """
        dlg = gui.SignInDialog(self.app)
        self.pump()
        variables = [getattr(dlg, name) for name in dlg.VARIABLES]
        self.assertTrue(all(v is not None for v in variables), "nothing to release?")

        dlg.destroy()
        for name in dlg.VARIABLES:
            self.assertIsNone(getattr(dlg, name), f"{name} outlived the dialog")

        # Sweep on this thread first, so the collect below cannot finalise a
        # stray variable left by an earlier test and blame it on this one.
        gc.collect()
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

    def test_the_dialog_survives_being_destroyed_twice(self):
        """The harness closes every dialog it opened; Close may have run already."""
        dlg = gui.SignInDialog(self.app)
        self.pump()
        dlg._close()
        dlg.destroy()

    def test_the_box_is_masked_until_asked_otherwise(self):
        dlg = self.dialog()
        self.assertEqual(dlg.entry.cget("show"), "•")
        dlg.reveal.set(True)
        dlg._sync_reveal()
        self.assertEqual(dlg.entry.cget("show"), "")

    def test_nothing_typed_means_nothing_to_save_or_test(self):
        dlg = self.dialog()
        self.assertEqual(str(dlg.save_button.cget("state")), "disabled")
        self.assertEqual(str(dlg.test_button.cget("state")), "disabled")
        dlg.token.set("x")
        self.pump()
        self.assertEqual(str(dlg.save_button.cget("state")), "normal")

    def test_signing_out_removes_it(self):
        dlg = self.dialog()
        dlg.token.set("github_pat_x")
        dlg._save()
        dlg._forget()
        self.pump()
        self.assertIsNone(core.github_token())
        self.assertIn("Not signed in", dlg.state_label.cget("text"))

    def test_sign_out_is_offered_only_when_there_is_something_to_remove(self):
        dlg = self.dialog()
        self.assertEqual(str(dlg.forget_button.cget("state")), "disabled")
        dlg.token.set("github_pat_x")
        dlg._save()
        self.pump()
        self.assertEqual(str(dlg.forget_button.cget("state")), "normal")

    def test_an_environment_token_cannot_be_signed_out_of_here(self):
        """It lives in the shell that launched us; this window cannot unset it.

        Greyed out rather than offered-and-ineffective: a Sign out that leaves
        you still signed in is worse than no button.
        """
        os.environ["GITHUB_TOKEN"] = "t"
        dlg = self.dialog()
        self.assertEqual(str(dlg.forget_button.cget("state")), "disabled")
        self.assertIn("GITHUB_TOKEN", dlg.state_label.cget("text"))

    def test_no_keyring_says_so_rather_than_claiming_one(self):
        """"Saved in your keyring" and "saved in a file" are different promises."""
        self.keyring_works = False
        dlg = self.dialog()
        dlg.token.set("github_pat_x")
        dlg._save()
        self.pump()
        self.assertIn("only you can read", dlg.state_label.cget("text"))
        self.assertEqual(core.github_token(), "github_pat_x")

    def test_typing_does_not_shell_out_once_per_character(self):
        """`token_source` reaches the keyring and `git credential fill`.

        Wired to the keystroke it was a subprocess per character, on the thread
        drawing the box -- which on a machine whose credential helper is slow
        is a text field that stops accepting text.
        """
        dlg = self.dialog()
        looks = []
        self.addCleanup(setattr, core, "token_source", core.token_source)
        core.token_source = lambda: (looks.append(1), None)[1]
        for count, char in enumerate("github_pat_1234567890", 1):
            dlg.token.set(dlg.token.get() + char)
            self.pump(1)
            self.assertEqual(looks, [], f"looked up again after {count} characters")

    def test_a_late_answer_from_an_earlier_ask_does_not_win(self):
        """The window asks at startup and again when the dialog closes.

        Both answers come back on their own worker, and the startup one --
        computed before the person signed in -- landing second would redraw the
        label with the state they had just changed.
        """
        self.app._sync_github()
        stale = self.app._github_asked
        self.secret["token"] = "t"
        core.forget_cached_token()
        self.app._sync_github()
        # The earlier worker, answering now, out of order.
        self.app.github.put((stale, None))
        for _ in range(60):
            self.pump(2)
            self.app._drain_github()
            if self.app.github_label.cget("text") != "checking…":
                break
        self.assertIn("secret store", self.app.github_label.cget("text"))

    def test_closing_the_window_cancels_the_token_lookup_timer(self):
        """A timer left armed prints `invalid command name` as the app exits."""
        self.app._sync_github()
        self.assertIsNotNone(self.app._github_after)
        self.app.stop()
        self.assertIsNone(self.app._github_after)

    def test_a_bad_token_is_reported_in_the_dialog_not_raised(self):
        """The Test button's whole job, and it must not take the window with it."""
        self.addCleanup(setattr, core, "token_identity", core.token_identity)
        core.token_identity = lambda token: core.die("GitHub does not recognise that token.")
        dlg = self.dialog()
        dlg.token.set("nope")
        dlg._test()
        for _ in range(40):
            self.pump(2)
            dlg._drain_checks()
            if "recognise" in dlg.result_label.cget("text"):
                break
        self.assertIn("recognise", dlg.result_label.cget("text"))
        self.assertEqual(str(dlg.test_button.cget("state")), "normal")

    def test_a_good_token_is_reported_with_the_account_it_belongs_to(self):
        self.addCleanup(setattr, core, "token_identity", core.token_identity)
        core.token_identity = lambda token: "Digigull"
        dlg = self.dialog()
        dlg.token.set("github_pat_x")
        dlg._test()
        for _ in range(40):
            self.pump(2)
            dlg._drain_checks()
            if "Digigull" in dlg.result_label.cget("text"):
                break
        self.assertIn("Digigull", dlg.result_label.cget("text"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
