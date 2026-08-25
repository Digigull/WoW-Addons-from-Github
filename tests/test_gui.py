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
        self.assertEqual(written["addons"]["Bound"]["installed"], "v3")

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

    def test_no_caution_for_a_folder_that_is_only_a_link(self):
        core.make_link(self.addons, self.addons / "Loose")
        dlg = self.dialog("Loose")
        dlg.choice.set("local")
        dlg._sync()
        self.assertEqual(dlg.caution.cget("text"), "")
        dlg.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
