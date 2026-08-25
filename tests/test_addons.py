#!/usr/bin/env python3
"""Offline tests for the engine in wowaddons.core.

    python3 -m unittest discover -s tests -t .

No network and no WoW install: the archive shapes are built in memory and the
GitHub API is stubbed. Runs in well under a second.

Per the repo's tooling rule this is not here for coverage -- it pins the two
things that actually broke while the tool was being written, plus the guards
whose failure would be silent:

  * an archive whose root IS the addon installed under GitHub's wrapper name
    (`someone-MyAddon-1a2b3c`), which the client ignores because it does not
    match the .toc inside
  * a 403 reported as "rate limit" when it was really a blocked proxy, sending
    you off to wait an hour for nothing
"""

import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wowaddons import core as addons  # noqa: E402


def mkzip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buf.getvalue()


class ArchiveLayouts(unittest.TestCase):
    """Whatever the archive looks like, AddOns must end up with Name/Name.toc.

    That is the exact condition the client uses to decide an addon exists, so
    it is the only assertion worth making here.
    """

    def install(self, files: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp)
            addons.install_zip(mkzip(files), target, dry_run=False)
            landed = sorted(child.name for child in target.iterdir())
            for name in landed:
                self.assertTrue((target / name / f"{name}.toc").is_file(), f"{name} is not loadable")
            return landed

    def test_packaged_release(self):
        self.assertEqual(self.install({"MyAddon/MyAddon.toc": "a"}), ["MyAddon"])

    def test_github_source_archive(self):
        self.assertEqual(self.install({"o-r-1a2b3c/MyAddon/MyAddon.toc": "a"}), ["MyAddon"])

    def test_repo_root_is_the_addon(self):
        # The regression: this used to install as `o-MyAddon-1a2b3c`.
        self.assertEqual(
            self.install({"o-MyAddon-1a2b3c/MyAddon.toc": "a", "o-MyAddon-1a2b3c/Core/core.lua": "x"}),
            ["MyAddon"],
        )

    def test_multi_folder_release(self):
        self.assertEqual(
            self.install({"MyAddon/MyAddon.toc": "a", "MyAddon_Config/MyAddon_Config.toc": "b"}),
            ["MyAddon", "MyAddon_Config"],
        )

    def test_flavour_suffixed_tocs_pick_the_base_name(self):
        self.assertEqual(self.install({"o-r-9f/MyAddon.toc": "a", "o-r-9f/MyAddon-Classic.toc": "b"}), ["MyAddon"])

    def test_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(addons.Fail):
                addons.install_zip(mkzip({"../evil/A/A.toc": "x"}), pathlib.Path(tmp), False)

    def test_junk_is_not_unpacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(addons.Fail):
                addons.install_zip(b"not a zip at all", pathlib.Path(tmp), False)


class SourceResolution(unittest.TestCase):
    def setUp(self):
        self.responses = {}
        self._real = addons.http_json
        addons.http_json = lambda url: self.responses.get(url)
        self.repo = "https://api.github.com/repos/o/r"

    def tearDown(self):
        addons.http_json = self._real

    def test_prefers_an_attached_zip_over_the_source_archive(self):
        self.responses[f"{self.repo}/releases/latest"] = {
            "tag_name": "v1.2.3",
            "zipball_url": f"{self.repo}/zipball/v1.2.3",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "http://x/notes.txt"},
                {"name": "MyAddon-1.2.3.zip", "browser_download_url": "http://x/MyAddon-1.2.3.zip"},
            ],
        }
        self.assertEqual(addons.latest_github("o/r"), ("v1.2.3", "http://x/MyAddon-1.2.3.zip"))

    def test_falls_back_to_the_source_archive(self):
        self.responses[f"{self.repo}/releases/latest"] = {
            "tag_name": "v2.0",
            "zipball_url": f"{self.repo}/zipball/v2.0",
            "assets": [],
        }
        self.assertEqual(addons.latest_github("o/r")[0], "v2.0")

    def test_no_releases_uses_the_default_branch_head(self):
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits/main"] = {"sha": "abcdef1234567890"}
        version, url = addons.latest_github("o/r")
        self.assertEqual(version, "abcdef123456")
        self.assertIn("/zipball/main", url)

    def test_explicit_branch(self):
        self.responses[f"{self.repo}/commits/dev"] = {"sha": "1122334455667788"}
        version, url = addons.latest_github("o/r@dev")
        self.assertEqual(version, "112233445566")
        self.assertIn("/zipball/dev", url)

    def test_missing_repo_is_reported(self):
        with self.assertRaises(addons.Fail):
            addons.latest_github("o/nope")


class ForbiddenIsNotAlwaysRateLimit(unittest.TestCase):
    """A 403 with quota left is a blocked proxy or a private repo, not a limit.

    The regression: every 403 said "wait an hour", which is the one piece of
    advice guaranteed not to help when the cause is egress or permissions.
    """

    def raise403(self, headers: dict, body: bytes):
        def fake(url, timeout=0):
            raise urllib.error.HTTPError(url, 403, "Forbidden", headers, io.BytesIO(body))

        return fake

    def test_quota_exhausted_says_rate_limit(self):
        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = self.raise403({"x-ratelimit-remaining": "0"}, b"{}")
        try:
            with self.assertRaises(addons.Fail) as caught:
                addons.http_json("https://api.github.com/repos/o/r")
            self.assertIn("rate limit", str(caught.exception).lower())
        finally:
            addons.urllib.request.urlopen = real

    def test_quota_remaining_surfaces_githubs_own_reason(self):
        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = self.raise403(
            {"x-ratelimit-remaining": "4999"}, b'{"message":"GitHub access is not enabled for this session."}'
        )
        try:
            with self.assertRaises(addons.Fail) as caught:
                addons.http_json("https://api.github.com/repos/o/r")
            message = str(caught.exception)
            self.assertIn("not enabled for this session", message)
            self.assertNotIn("rate limit", message.lower())
        finally:
            addons.urllib.request.urlopen = real


class TocReading(unittest.TestCase):
    def test_colour_escapes_are_stripped_from_titles(self):
        self.assertEqual(addons.strip_colours("|cff33ff99Some|r Addon"), "Some Addon")

    def test_a_github_website_header_becomes_a_source(self):
        self.assertEqual(
            addons.guess_source({"x-website": "https://github.com/someone/SomeAddon"}),
            "github:someone/SomeAddon",
        )

    def test_a_non_github_website_suggests_nothing(self):
        self.assertIsNone(addons.guess_source({"x-website": "https://curseforge.com/wow/x"}))


class UpdatingOneAddon(unittest.TestCase):
    """`update_addon` is what both front ends call, so its contract is the API.

    The rule worth pinning is the one the whole tool rests on: a failure comes
    back as a FAILED result, never as an exception, because an exception here
    would abort the run and discard every manifest change made before it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self._latest = addons.latest_github
        self._download = addons.download
        addons.latest_github = lambda spec: ("v2", "http://x/a.zip")
        addons.download = lambda url: mkzip({"MyAddon/MyAddon.toc": "a"})

    def tearDown(self):
        addons.latest_github = self._latest
        addons.download = self._download
        self.tmp.cleanup()

    def entry(self, **over):
        base = {"source": "github:o/r", "mode": "link", "installed": None, "folders": []}
        base.update(over)
        return base

    def test_unmanaged_is_skipped_not_failed(self):
        result = addons.update_addon("A", self.entry(source="unmanaged"), self.root)
        self.assertEqual(result.outcome, addons.UNMANAGED)

    def test_a_new_version_installs_and_records_itself(self):
        entry = self.entry()
        result = addons.update_addon("MyAddon", entry, self.root)
        self.assertEqual(result.outcome, addons.CHANGED)
        self.assertEqual(entry["installed"], "v2")
        self.assertTrue((self.root / "MyAddon" / "MyAddon.toc").is_file())

    def test_matching_version_is_left_alone(self):
        entry = self.entry(installed="v2")
        result = addons.update_addon("MyAddon", entry, self.root)
        self.assertEqual(result.outcome, addons.UP_TO_DATE)
        self.assertFalse((self.root / "MyAddon").exists())

    def test_force_reinstalls_a_matching_version(self):
        entry = self.entry(installed="v2")
        self.assertEqual(addons.update_addon("MyAddon", entry, self.root, force=True).outcome, addons.CHANGED)
        self.assertTrue((self.root / "MyAddon").is_dir())

    def test_check_reports_without_downloading(self):
        addons.download = lambda url: self.fail("check must not download")
        entry = self.entry()
        result = addons.update_addon("MyAddon", entry, self.root, check=True)
        self.assertEqual(result.outcome, addons.CHANGED)
        self.assertIsNone(entry["installed"], "check must not record an install")

    def test_dry_run_writes_nothing(self):
        entry = self.entry()
        result = addons.update_addon("MyAddon", entry, self.root, dry_run=True)
        self.assertEqual(result.folders, ["MyAddon"], "it should still say what it would install")
        self.assertFalse((self.root / "MyAddon").exists())
        self.assertIsNone(entry["installed"])

    def test_a_failure_is_a_result_not_an_exception(self):
        # The regression this guards: raising here aborted the whole update and
        # dropped the manifest changes made by the addons ahead of this one.
        def unreachable(spec):
            addons.die("could not reach GitHub: connection refused")

        addons.latest_github = unreachable
        entry = self.entry(installed="v1")
        result = addons.update_addon("MyAddon", entry, self.root)
        self.assertEqual(result.outcome, addons.FAILED)
        self.assertIn("connection refused", result.detail)
        self.assertEqual(entry["installed"], "v1", "a failed update must not rewrite the entry")

    def test_a_local_source_links_and_survives_a_second_run(self):
        source = pathlib.Path(self.tmp.name) / "src" / "MyAddon"
        source.mkdir(parents=True)
        (source / "MyAddon.toc").write_text("## Title: x")
        entry = self.entry(source=f"local:{source}")

        for _ in range(2):
            result = addons.update_addon("MyAddon", entry, self.root)
            self.assertEqual(result.outcome, addons.CHANGED)
            # is_link, not Path.is_symlink: on Windows this installs a junction,
            # and Path.is_symlink() reports False for one. See LinkingWithoutPrivileges.
            self.assertTrue(addons.is_link(self.root / "MyAddon"))
        self.assertEqual(entry["installed"], "linked")


class DisplacingRealFiles(unittest.TestCase):
    """Binding over a real folder moves it aside, and the GUI has to say so first.

    In a terminal you read the log afterwards. In a window you are told in the
    confirm step or you never find out, so the name has to be computable before
    anything is moved.
    """

    def test_the_backup_name_does_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "MyAddon"
            target.mkdir()
            self.assertEqual(addons.backup_name(target).name, "MyAddon.replaced")
            (pathlib.Path(tmp) / "MyAddon.replaced").mkdir()
            self.assertEqual(addons.backup_name(target).name, "MyAddon.replaced2")

    def test_a_real_folder_is_reported_as_displaced_and_then_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "AddOns"
            root.mkdir()
            (root / "MyAddon").mkdir()
            (root / "MyAddon" / "old.lua").write_text("old")
            source = pathlib.Path(tmp) / "src" / "MyAddon"
            source.mkdir(parents=True)
            (source / "MyAddon.toc").write_text("## Title: x")

            entry = {"source": f"local:{source}", "mode": "link", "installed": None, "folders": []}
            warned = addons.will_displace(entry, root)
            self.assertEqual(warned.name, "MyAddon.replaced")

            addons.update_addon("MyAddon", entry, root)
            self.assertTrue((root / "MyAddon.replaced" / "old.lua").is_file(), "the old files must survive")
            self.assertTrue(addons.is_link(root / "MyAddon"))

    def test_a_symlink_is_not_reported_as_displaced(self):
        # Replacing a link destroys nothing, so warning about it would be noise.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "AddOns"
            root.mkdir()
            source = pathlib.Path(tmp) / "src" / "MyAddon"
            source.mkdir(parents=True)
            # Made the way the tool makes them, so this exercises a junction on
            # Windows rather than a symlink the tool would never create.
            addons.make_link(source, root / "MyAddon")
            entry = {"source": f"local:{source}", "mode": "link"}
            self.assertIsNone(addons.will_displace(entry, root))


class LinkingWithoutPrivileges(unittest.TestCase):
    """A `local:` source installs as a link. On Windows that has to be a junction.

    os.symlink needs administrator rights or Developer Mode there, which is not
    a reasonable thing to ask of somebody who wants to update an addon. A
    directory junction needs neither and the client cannot tell the difference.

    These run on every platform on purpose: the CI matrix includes
    windows-latest, so this is the code path actually being exercised there, not
    a reasoned-about one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "keep.lua").write_text("precious")

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_is_symlink_is_not_enough_on_its_own(self):
        """What Windows CI answered, rather than what seemed likely.

        The plan flagged this as "verify on a real Windows box, not by
        reasoning", and the answer is that Path.is_symlink() reports False for a
        directory junction on both Python 3.9 and 3.12. is_link() therefore
        cannot delegate to it -- which matters because callers use the answer to
        decide whether shutil.rmtree is safe.

        Off Windows the two agree, and this asserts that much everywhere.
        """
        link = self.root / "link"
        addons.make_link(self.source, link)
        try:
            self.assertTrue(addons.is_link(link))
            if os.name != "nt":
                self.assertTrue(link.is_symlink())
        finally:
            addons.remove_link(link)

    def test_a_link_round_trips(self):
        link = self.root / "link"
        addons.make_link(self.source, link)
        self.assertTrue(addons.is_link(link), "a link this tool made must read back as one")
        self.assertTrue((link / "keep.lua").is_file(), "the client has to see through it")
        self.assertEqual(pathlib.Path(addons.link_target(link)), self.source)

        addons.remove_link(link)
        self.assertFalse(link.exists())
        self.assertTrue((self.source / "keep.lua").is_file(), "detaching must not touch the target")

    def test_a_real_directory_is_not_a_link(self):
        self.assertFalse(addons.is_link(self.source))
        self.assertIsNone(addons.link_target(self.source))

    def test_is_link_does_not_raise_on_something_that_is_not_there(self):
        self.assertFalse(addons.is_link(self.root / "nope"))

    def test_replacing_a_link_does_not_delete_through_it(self):
        # The one that would be unrecoverable. Everything that replaces an
        # installed addon asks is_link() first and calls shutil.rmtree if the
        # answer is no -- so an is_link() that misses a Windows junction means
        # rmtree walks through it and deletes the user's source checkout.
        addons_dir = self.root / "AddOns"
        addons_dir.mkdir()
        addons.make_link(self.source, addons_dir / "source")

        addons.install_local(self.source, addons_dir, "link", dry_run=False)

        self.assertTrue((self.source / "keep.lua").is_file(), "the source checkout must survive")
        self.assertTrue(addons.is_link(addons_dir / "source"))

    def test_an_archive_can_replace_a_linked_addon(self):
        addons_dir = self.root / "AddOns"
        addons_dir.mkdir()
        source = self.root / "MyAddon"
        source.mkdir()
        (source / "keep.lua").write_text("precious")
        addons.make_link(source, addons_dir / "MyAddon")

        addons.install_zip(mkzip({"MyAddon/MyAddon.toc": "a"}), addons_dir, dry_run=False)

        self.assertTrue((source / "keep.lua").is_file(), "the source checkout must survive")
        self.assertFalse(addons.is_link(addons_dir / "MyAddon"), "it is real files now")
        self.assertTrue((addons_dir / "MyAddon" / "MyAddon.toc").is_file())

    def test_a_scan_reports_a_linked_addon_as_linked(self):
        addons_dir = self.root / "AddOns"
        addons_dir.mkdir()
        addon = self.root / "MyAddon"
        addon.mkdir()
        (addon / "MyAddon.toc").write_text("## Title: Mine")
        addons.make_link(addon, addons_dir / "MyAddon")

        found = addons.scan_installed(addons_dir)
        self.assertTrue(found["MyAddon"]["is_link"])
        self.assertEqual(pathlib.Path(found["MyAddon"]["link_target"]), addon)

    def test_a_link_target_has_no_extended_length_prefix(self):
        # readlink on a junction hands back \\?\C:\... which is correct and
        # is not what anyone typed; it should not be what a listing shows.
        link = self.root / "link"
        addons.make_link(self.source, link)
        try:
            self.assertFalse(addons.link_target(link).startswith("\\\\?\\"))
        finally:
            addons.remove_link(link)


class WhereTheManifestLives(unittest.TestCase):
    """%APPDATA% on Windows, $XDG_CONFIG_HOME elsewhere.

    Windows landing in ~/.config worked but is not a place a Windows user, or
    their backup software, would ever think to look.
    """

    def setUp(self):
        self.environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environ)

    def test_windows_uses_appdata(self):
        os.environ["APPDATA"] = os.path.join("C:" + os.sep, "Users", "you", "AppData", "Roaming")
        where = str(addons.default_config_dir(windows=True))
        self.assertTrue(where.endswith("wow-addons"), where)
        self.assertIn("Roaming", where)
        self.assertNotIn(".config", where)

    def test_windows_falls_back_when_appdata_is_unset(self):
        os.environ.pop("APPDATA", None)
        self.assertIn("Roaming", str(addons.default_config_dir(windows=True)))

    def test_elsewhere_uses_xdg(self):
        os.environ["XDG_CONFIG_HOME"] = os.path.join(os.sep, "somewhere", "config")
        self.assertEqual(
            addons.default_config_dir(windows=False),
            pathlib.Path(os.sep, "somewhere", "config", "wow-addons"),
        )

    def test_xdg_defaults_to_dot_config(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.assertEqual(
            addons.default_config_dir(windows=False), pathlib.Path.home() / ".config" / "wow-addons"
        )

    def test_the_old_windows_location_is_still_read(self):
        # Upgrading must not look like "you have no addons bound".
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            legacy = tmp / "legacy.json"
            legacy.write_text('{"addons_dir": "/old/place", "addons": {}}')
            new, old_legacy = addons.MANIFEST, addons.LEGACY_WINDOWS_MANIFEST
            try:
                addons.MANIFEST = tmp / "new.json"
                addons.LEGACY_WINDOWS_MANIFEST = legacy
                self.assertEqual(addons.load(windows=True)["addons_dir"], "/old/place")

                # And once something is written to the new place, that wins.
                addons.MANIFEST.write_text('{"addons_dir": "/new/place", "addons": {}}')
                self.assertEqual(addons.load(windows=True)["addons_dir"], "/new/place")
            finally:
                addons.MANIFEST, addons.LEGACY_WINDOWS_MANIFEST = new, old_legacy

    def test_the_old_location_is_not_consulted_off_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            legacy = tmp / "legacy.json"
            legacy.write_text('{"addons_dir": "/old/place", "addons": {}}')
            new, old_legacy = addons.MANIFEST, addons.LEGACY_WINDOWS_MANIFEST
            try:
                addons.MANIFEST = tmp / "new.json"
                addons.LEGACY_WINDOWS_MANIFEST = legacy
                self.assertIsNone(addons.load(windows=False)["addons_dir"])
            finally:
                addons.MANIFEST, addons.LEGACY_WINDOWS_MANIFEST = new, old_legacy


class ReplacingWhatIsAlreadyThere(unittest.TestCase):
    """What happens to files that are already in AddOns, for BOTH source kinds.

    Reported from a real Debian install: the user bound an addon to a GitHub
    repo, the window told them the folder would be moved to `<Name>.replaced`,
    they updated, and there was no such folder anywhere. There never had been.

    install_zip deleted with shutil.rmtree and install_local renamed, so the
    answer to "will this destroy my addon?" depended on which kind of source you
    happened to pick -- while the window gave the same answer for both. These
    tests exist so that can never diverge again.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "AddOns"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def hand_installed(self, name="MyAddon"):
        """A folder the user put there themselves, with something worth keeping."""
        folder = self.root / name
        folder.mkdir()
        (folder / f"{name}.toc").write_text("## Title: mine")
        (folder / "settings.lua").write_text("years of config")
        return folder

    def source_checkout(self, name="MyAddon"):
        checkout = pathlib.Path(self.tmp.name) / "src" / name
        checkout.mkdir(parents=True)
        (checkout / f"{name}.toc").write_text("## Title: from the repo")
        return checkout

    # -- the bug ------------------------------------------------------------

    def test_an_archive_install_keeps_what_it_replaces(self):
        # The regression. This used to rmtree the folder outright.
        self.hand_installed()
        entry = {"source": "github:o/r", "mode": "link", "installed": None, "folders": []}
        addons.install_zip(
            mkzip({"MyAddon/MyAddon.toc": "new"}), self.root, dry_run=False,
            backup=addons.should_backup(entry),
        )
        kept = self.root / "MyAddon.replaced"
        self.assertTrue(kept.is_dir(), "the user's folder was deleted, not kept")
        self.assertEqual((kept / "settings.lua").read_text(), "years of config")
        self.assertTrue((self.root / "MyAddon" / "MyAddon.toc").is_file())

    def test_both_source_kinds_agree_about_keeping_files(self):
        # The point is not that each is right in isolation; it is that they
        # answer the same question the same way.
        for kind in ("archive", "local"):
            with self.subTest(kind=kind):
                for stale in self.root.iterdir():
                    shutil.rmtree(stale) if stale.is_dir() and not stale.is_symlink() else stale.unlink()
                self.hand_installed()
                entry = {"installed": None, "backup": True}
                if kind == "archive":
                    addons.install_zip(mkzip({"MyAddon/MyAddon.toc": "new"}), self.root,
                                       dry_run=False, backup=addons.should_backup(entry))
                else:
                    addons.install_local(self.source_checkout(), self.root, "copy",
                                         dry_run=False, backup=addons.should_backup(entry))
                self.assertTrue((self.root / "MyAddon.replaced" / "settings.lua").is_file(),
                                f"{kind} did not keep the user's files")

    # -- backing up once, not every time ------------------------------------

    def test_a_folder_this_tool_installed_is_replaced_without_a_copy(self):
        # The complaint that started this: .replaced2, .replaced3 piling up.
        # Anything with a recorded version came from the source, so there is
        # nothing of the user's in it to keep.
        self.assertFalse(addons.should_backup({"installed": "v1"}))
        self.assertTrue(addons.should_backup({"installed": None}))

    def test_repeated_copy_updates_do_not_accumulate_backups(self):
        self.hand_installed()
        checkout = self.source_checkout()
        entry = {"source": f"local:{checkout}", "mode": "copy", "installed": None, "folders": []}

        for _ in range(4):
            addons.update_addon("MyAddon", entry, self.root)

        kept = sorted(p.name for p in self.root.iterdir() if ".replaced" in p.name)
        self.assertEqual(kept, ["MyAddon.replaced"], f"backups accumulated: {kept}")
        self.assertEqual((self.root / "MyAddon.replaced" / "settings.lua").read_text(),
                         "years of config", "the ONE kept copy must be the user's original")

    def test_turning_the_backup_off_replaces_outright(self):
        self.hand_installed()
        entry = {"source": "github:o/r", "mode": "link", "installed": None,
                 "folders": [], "backup": False}
        self.assertFalse(addons.should_backup(entry))
        addons.install_zip(mkzip({"MyAddon/MyAddon.toc": "new"}), self.root,
                           dry_run=False, backup=addons.should_backup(entry))
        self.assertEqual([p.name for p in self.root.iterdir() if "replaced" in p.name], [])
        self.assertTrue((self.root / "MyAddon" / "MyAddon.toc").is_file())

    # -- what the window is told --------------------------------------------

    def test_the_warning_matches_what_actually_happens(self):
        """displaced_folder and should_backup are what the dialog asks.

        The original bug was the window promising one thing while core did
        another, so these must be answered from the same place the install is.
        """
        self.hand_installed()
        for source in ("github:o/r", None):
            entry = {"source": source or f"local:{self.source_checkout()}", "installed": None}
            with self.subTest(source=entry["source"]):
                doomed = addons.displaced_folder(entry, "MyAddon", self.root)
                self.assertIsNotNone(doomed, "the window would have shown no warning")
                self.assertEqual(doomed.name, "MyAddon")
                self.assertEqual(addons.will_displace(entry, self.root, "MyAddon").name,
                                 "MyAddon.replaced")

    def test_nothing_is_promised_when_nothing_will_be_kept(self):
        self.hand_installed()
        entry = {"source": "github:o/r", "installed": None, "backup": False}
        self.assertIsNotNone(addons.displaced_folder(entry, "MyAddon", self.root),
                             "a folder IS about to be destroyed")
        self.assertIsNone(addons.will_displace(entry, self.root, "MyAddon"),
                          "but nothing is kept, so nothing may be promised")


class PastingARepoAddress(unittest.TestCase):
    """Whatever is on the clipboard when somebody means "this addon".

    Reported from a real install: pasting the repository URL was refused and
    demanded owner/repo. Telling somebody who just pasted a working link to
    retype it by hand is a small insult with no reason behind it.
    """

    def test_the_shapes_people_actually_paste(self):
        for text, expected in [
            ("tullamods/Bagnon", ("tullamods/Bagnon", None)),
            ("https://github.com/tullamods/Bagnon", ("tullamods/Bagnon", None)),
            ("https://github.com/tullamods/Bagnon/", ("tullamods/Bagnon", None)),
            ("https://github.com/tullamods/Bagnon.git", ("tullamods/Bagnon", None)),
            ("http://www.github.com/tullamods/Bagnon", ("tullamods/Bagnon", None)),
            ("github.com/tullamods/Bagnon", ("tullamods/Bagnon", None)),
            ("git@github.com:tullamods/Bagnon.git", ("tullamods/Bagnon", None)),
            ("  https://github.com/tullamods/Bagnon  ", ("tullamods/Bagnon", None)),
            ("https://github.com/tullamods/Bagnon#readme", ("tullamods/Bagnon", None)),
        ]:
            with self.subTest(text=text):
                self.assertEqual(addons.parse_repo(text), expected)

    def test_a_branch_in_the_url_is_kept(self):
        # Somebody browsing a branch and copying the address means that branch.
        self.assertEqual(addons.parse_repo("https://github.com/Questie/Questie/tree/develop"),
                         ("Questie/Questie", "develop"))
        self.assertEqual(addons.parse_repo("https://github.com/o/r/blob/main"), ("o/r", "main"))

    def test_things_that_are_not_a_github_repo_are_refused(self):
        # Refused, not mangled: storing a CurseForge page as owner/repo would
        # produce a source that can never resolve, and a confusing 404 later.
        for text in ("https://curseforge.com/wow/addons/bagnon", "not a repo",
                     "https://gitlab.com/o/r", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(addons.parse_repo(text))

    def test_a_pasted_url_survives_being_set_as_a_source(self):
        state = {"addons": {}}
        entry, _ = addons.set_source(state, "Bagnon", "github:https://github.com/tullamods/Bagnon")
        self.assertEqual(entry["source"], "github:tullamods/Bagnon")

    def test_a_pasted_branch_url_survives_too(self):
        state = {"addons": {}}
        entry, _ = addons.set_source(state, "Questie", "github:https://github.com/Questie/Questie/tree/develop")
        self.assertEqual(entry["source"], "github:Questie/Questie@develop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
