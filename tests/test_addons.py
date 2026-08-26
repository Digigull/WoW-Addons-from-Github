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

import datetime
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wowaddons import core as addons  # noqa: E402


def setUpModule():
    """No test in this file may reach the network, including through git.

    The engine now asks github.com for a repository's refs before it asks the
    REST API anything, because that request is not billed against the hourly
    quota. Left alone here it is still a real request, to a repository called
    `o/r` that does not exist -- fifty of them across this file, slow and
    dependent on somebody's connection, in a suite whose first promise is that
    it runs offline.

    Stubbed to "could not find out", which is exactly what an offline machine
    would get, and which sends every caller down the REST path these tests were
    written against. The tests that are *about* the shortcut put their own stub
    in and take it out again.
    """
    global _real_git_refs
    _real_git_refs = addons.git_refs
    addons.git_refs = lambda repo: None


def tearDownModule():
    addons.git_refs = _real_git_refs


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
        self.responses[f"{self.repo}/releases?per_page=1"] = [{
            "tag_name": "v1.2.3",
            "zipball_url": f"{self.repo}/zipball/v1.2.3",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "http://x/notes.txt"},
                {"name": "MyAddon-1.2.3.zip", "browser_download_url": "http://x/MyAddon-1.2.3.zip"},
            ],
        }]
        self.assertEqual(addons.latest_github("o/r"), ("v1.2.3", "http://x/MyAddon-1.2.3.zip"))

    def test_falls_back_to_the_source_archive(self):
        self.responses[f"{self.repo}/releases?per_page=1"] = [{
            "tag_name": "v2.0",
            "zipball_url": f"{self.repo}/zipball/v2.0",
            "assets": [],
        }]
        # The release's own zipball_url is a REST call; the tag is fetched off
        # the meter instead.
        self.assertEqual(addons.latest_github("o/r"),
                         ("v2.0", "https://codeload.github.com/o/r/zip/refs/tags/v2.0"))

    def test_no_releases_uses_the_default_branch_head(self):
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits/main"] = {"sha": "abcdef1234567890"}
        version, url = addons.latest_github("o/r")
        self.assertEqual(version, "abcdef123456")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/main")

    def test_explicit_branch(self):
        self.responses[f"{self.repo}/commits/dev"] = {"sha": "1122334455667788"}
        version, url = addons.latest_github("o/r@dev")
        self.assertEqual(version, "112233445566")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/dev")

    def test_missing_repo_is_reported(self):
        with self.assertRaises(addons.Fail):
            addons.latest_github("o/nope")


class ForbiddenIsNotAlwaysRateLimit(unittest.TestCase):
    """A 403 with quota left is a blocked proxy or a private repo, not a limit.

    The regression: every 403 said "wait an hour", which is the one piece of
    advice guaranteed not to help when the cause is egress or permissions.
    """

    def setUp(self):
        # The engine remembers what the last response said about the quota, so
        # a test that hands it an exhausted one has to hand it back.
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)

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
                self.assertEqual(addons.current(addons.load(windows=True))["addons_dir"], "/old/place")

                # And once something is written to the new place, that wins.
                addons.MANIFEST.write_text('{"addons_dir": "/new/place", "addons": {}}')
                self.assertEqual(addons.current(addons.load(windows=True))["addons_dir"], "/new/place")
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
                self.assertIsNone(addons.current(addons.load(windows=False))["addons_dir"])
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
            ("tullamods/Bagnon", ("tullamods/Bagnon", None, None)),
            ("https://github.com/tullamods/Bagnon", ("tullamods/Bagnon", None, None)),
            ("https://github.com/tullamods/Bagnon/", ("tullamods/Bagnon", None, None)),
            ("https://github.com/tullamods/Bagnon.git", ("tullamods/Bagnon", None, None)),
            ("http://www.github.com/tullamods/Bagnon", ("tullamods/Bagnon", None, None)),
            ("github.com/tullamods/Bagnon", ("tullamods/Bagnon", None, None)),
            ("git@github.com:tullamods/Bagnon.git", ("tullamods/Bagnon", None, None)),
            ("  https://github.com/tullamods/Bagnon  ", ("tullamods/Bagnon", None, None)),
            ("https://github.com/tullamods/Bagnon#readme", ("tullamods/Bagnon", None, None)),
            # A repo of several addons: the folder you clicked into IS the
            # statement of which addon you mean, and it is what gets pasted.
            ("https://github.com/Digigull/Ascension-Custom-Addons/tree/main/AscensionHonorTracker",
             ("Digigull/Ascension-Custom-Addons", "main", "AscensionHonorTracker")),
            ("https://github.com/o/r/tree/main/Nested/Deep/Addon/",
             ("o/r", "main", "Nested/Deep/Addon")),
            ("o/r#OneAddon", ("o/r", None, "OneAddon")),
        ]:
            with self.subTest(text=text):
                self.assertEqual(addons.parse_repo(text), expected)

    def test_a_branch_in_the_url_is_kept(self):
        # Somebody browsing a branch and copying the address means that branch.
        self.assertEqual(addons.parse_repo("https://github.com/Questie/Questie/tree/develop"),
                         ("Questie/Questie", "develop", None))
        self.assertEqual(addons.parse_repo("https://github.com/o/r/blob/main"), ("o/r", "main", None))

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


# A repository holding several unrelated addons -- one commit history, nine
# addons, no releases. Every assumption below was checked against a real one:
# Digigull/Ascension-Custom-Addons, which is where these bugs were found.
MONOREPO = {
    "Ascension-Custom-Addons-1a2b3c/AscensionHonorTracker/AscensionHonorTracker.toc": "a",
    "Ascension-Custom-Addons-1a2b3c/GnomeWorks/GnomeWorks.toc": "b",
    "Ascension-Custom-Addons-1a2b3c/TurboPlates/TurboPlates.toc": "c",
    "Ascension-Custom-Addons-1a2b3c/README.md": "docs",
}


class OneAddonOutOfMany(unittest.TestCase):
    """Binding an addon to one folder of a repository that holds several.

    Without this, binding a single addon to such a repo installs all of them,
    and the entry then claims every one of their folders as its own. Both
    halves cause damage, and the second one destroyed a file during testing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_only_the_bound_folder_is_installed(self):
        written = addons.install_zip(mkzip(MONOREPO), self.root, dry_run=False,
                                     only="AscensionHonorTracker")
        self.assertEqual(written, ["AscensionHonorTracker"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["AscensionHonorTracker"])

    def test_without_a_folder_the_whole_lot_still_installs(self):
        # The existing behaviour, kept: an addon that ships its own library
        # depends on it, and most repos hold exactly one addon anyway.
        written = addons.install_zip(mkzip(MONOREPO), self.root, dry_run=False)
        self.assertEqual(written,
                         ["AscensionHonorTracker", "GnomeWorks", "TurboPlates"])

    def test_a_folder_that_is_not_there_is_refused_by_name(self):
        with self.assertRaises(addons.Fail) as caught:
            addons.install_zip(mkzip(MONOREPO), self.root, False, only="Nonexistent")
        self.assertIn("Nonexistent", str(caught.exception))

    def test_a_folder_this_tool_did_not_write_is_kept(self):
        """The bug that deleted a file, reduced to its shape.

        Bind one addon of a repo that holds three. The entry records the folder
        it installed. A different addon from that same repo is then put there by
        hand -- and the next update must not treat it as one of ours just
        because the same archive happens to contain a folder by that name.

        This used to decide once, from the entry, and apply that answer to every
        folder the archive landed.
        """
        entry = {"backup": True, "installed": "v1", "folders": ["AscensionHonorTracker"]}
        mine = self.root / "GnomeWorks"
        mine.mkdir()
        (mine / "GnomeWorks.toc").write_text("mine")
        (mine / "my_edit.lua").write_text("an afternoon's work")

        addons.install_zip(mkzip(MONOREPO), self.root, False, backup=True, entry=entry)

        kept = self.root / "GnomeWorks.replaced" / "my_edit.lua"
        self.assertTrue(kept.is_file(), "a folder this tool never installed was destroyed")
        self.assertEqual(kept.read_text(), "an afternoon's work")

    def test_a_folder_this_tool_did_write_is_replaced_without_piling_up(self):
        # The other half of the same rule: our own folder is replaced directly,
        # or .replaced2, .replaced3 accumulate on every update.
        entry = {"backup": True, "installed": "v1", "folders": ["AscensionHonorTracker"]}
        addons.install_zip(mkzip(MONOREPO), self.root, False, backup=True, entry=entry,
                           only="AscensionHonorTracker")
        addons.install_zip(mkzip(MONOREPO), self.root, False, backup=True, entry=entry,
                           only="AscensionHonorTracker")
        self.assertEqual([p.name for p in self.root.glob("*.replaced*")], [])

    def test_should_backup_folder_answers_per_folder(self):
        entry = {"backup": True, "installed": "v1", "folders": ["Mine"]}
        self.assertFalse(addons.should_backup_folder(entry, "Mine"))
        self.assertTrue(addons.should_backup_folder(entry, "Theirs"))
        # Nothing installed yet: the folder is the user's whatever it is called.
        self.assertTrue(addons.should_backup_folder({"folders": ["Mine"]}, "Mine"))
        # And an explicit no still means no.
        self.assertFalse(addons.should_backup_folder({"backup": False}, "Anything"))


class VersionFollowsTheFolder(unittest.TestCase):
    """A mono-repo's HEAD moves when any addon in it changes.

    Versioning an addon by the repository reports an update for all nine every
    time one of them is touched. An "update available" that is usually wrong is
    worse than no column at all, because people stop reading it.
    """

    def setUp(self):
        self.responses = {}
        self._real = addons.http_json
        addons.http_json = lambda url: self.responses.get(url)
        self.addCleanup(lambda: setattr(addons, "http_json", self._real))
        self.repo = "https://api.github.com/repos/o/r"

    def test_the_version_is_the_last_commit_touching_that_folder(self):
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits?sha=main&path=HonorTracker&per_page=1"] = [
            {"sha": "9ba7d5f00000000"}
        ]
        version, url = addons.latest_github("o/r#HonorTracker")
        self.assertEqual(version, "9ba7d5f00000")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/main")

    def test_two_folders_in_one_repo_get_different_versions(self):
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits?sha=main&path=A&per_page=1"] = [{"sha": "aaaaaaaaaaaa"}]
        self.responses[f"{self.repo}/commits?sha=main&path=B&per_page=1"] = [{"sha": "bbbbbbbbbbbb"}]
        self.assertNotEqual(addons.latest_github("o/r#A")[0], addons.latest_github("o/r#B")[0])

    def test_a_named_folder_is_not_overruled_by_a_release(self):
        # A release asset is packaged for one addon; nothing says its contents
        # line up with a path in the source tree, so honouring both would mean
        # guessing which the user meant.
        self.responses[f"{self.repo}/releases?per_page=1"] = [{
            "tag_name": "v9.9", "zipball_url": "z", "assets": [],
        }]
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits?sha=main&path=A&per_page=1"] = [{"sha": "cccccccccccc"}]
        self.assertEqual(addons.latest_github("o/r#A")[0], "cccccccccccc")

    def test_a_branch_and_a_folder_together(self):
        self.responses[f"{self.repo}/commits?sha=dev&path=A&per_page=1"] = [{"sha": "dddddddddddd"}]
        version, url = addons.latest_github("o/r@dev#A")
        self.assertEqual(version, "dddddddddddd")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/dev")

    def test_a_folder_nothing_ever_touched_is_reported(self):
        self.responses[self.repo] = {"default_branch": "main"}
        self.responses[f"{self.repo}/commits?sha=main&path=Typo&per_page=1"] = []
        with self.assertRaises(addons.Fail) as caught:
            addons.latest_github("o/r#Typo")
        self.assertIn("Typo", str(caught.exception))


class RepoLayoutsThatUsedToBeMissed(unittest.TestCase):
    """Addons are not always at the top of the tree."""

    def install(self, files, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            return addons.install_zip(mkzip(files), pathlib.Path(tmp), dry_run=True, **kw)

    def test_an_addon_under_src_beside_docs(self):
        # Nothing recognisable at the top and more than one way down, so the
        # old search gave up and reported "no addon folder found".
        self.assertEqual(
            self.install({"r-1a2b/src/MyAddon/MyAddon.toc": "x", "r-1a2b/docs/readme.md": "y"}),
            ["MyAddon"],
        )

    def test_a_dot_directory_is_not_searched(self):
        self.assertEqual(
            self.install({"r-1a2b/MyAddon/MyAddon.toc": "x",
                          "r-1a2b/.github/workflows/ci.yml": "y"}),
            ["MyAddon"],
        )

    def test_bundled_libraries_do_not_become_the_addon(self):
        # An addon shipping Libs/AceGUI-3.0/AceGUI-3.0.toc must install as
        # MyAddon, not as its own libraries. This is why the deeper search is
        # a last resort and bounded rather than a full walk.
        self.assertEqual(
            self.install({"r-1a2b/MyAddon/MyAddon.toc": "x",
                          "r-1a2b/MyAddon/Libs/AceGUI-3.0/AceGUI-3.0.toc": "y"}),
            ["MyAddon"],
        )


class SourcesNamingAFolder(unittest.TestCase):
    def test_a_folder_url_is_what_people_will_paste(self):
        # Clicking into one addon of several on github.com and copying the
        # address is the clearest statement of which addon is meant.
        self.assertEqual(
            addons.parse_repo("https://github.com/o/r/tree/main/HonorTracker"),
            ("o/r", "main", "HonorTracker"),
        )

    def test_it_round_trips_through_set_source(self):
        state = {"addons": {}}
        entry, _ = addons.set_source(
            state, "HonorTracker",
            "github:https://github.com/o/r/tree/main/HonorTracker",
        )
        self.assertEqual(entry["source"], "github:o/r@main#HonorTracker")
        self.assertEqual(addons.split_repo_spec("o/r@main#HonorTracker"),
                         ("o/r", "main", "HonorTracker"))

    def test_an_ssh_url_is_not_split_on_its_own_at_sign(self):
        # git@github.com:o/r used to split into repo "git", branch
        # "github.com:o/r", because the "@" was taken before the URL was read.
        state = {"addons": {}}
        entry, _ = addons.set_source(state, "Bagnon", "git@github.com:tullamods/Bagnon.git")
        self.assertEqual(entry["source"], "github:tullamods/Bagnon")

    def test_the_folder_is_split_before_the_branch(self):
        # A branch may not contain "#", but a path may well contain "@".
        self.assertEqual(addons.split_repo_spec("o/r#weird@name"), ("o/r", None, "weird@name"))


class UpdatingOneAddonOfManyEndToEnd(unittest.TestCase):
    """The whole chain, because the bug lived in how the parts were joined.

    Each piece was defensible on its own: `should_backup` correctly said "this
    tool installed this addon, replacing it loses nothing", and `install_zip`
    correctly applied the flag it was given. The damage was that one answer
    about one addon was handed to a loop over nine folders.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._latest, self._download = addons.latest_github, addons.download
        addons.latest_github = lambda spec: ("v2", "http://x/a.zip")
        addons.download = lambda url: mkzip(MONOREPO)
        self.addCleanup(lambda: (setattr(addons, "latest_github", self._latest),
                                 setattr(addons, "download", self._download)))

    def test_a_hand_installed_addon_survives_updating_a_neighbour(self):
        entry = {
            "source": "github:o/r",          # the whole repo, as it was bound
            "installed": "v1",               # and updated once already
            "folders": ["AscensionHonorTracker"],
            "backup": True,
        }
        mine = self.root / "GnomeWorks"
        mine.mkdir()
        (mine / "GnomeWorks.toc").write_text("mine")
        (mine / "my_edit.lua").write_text("an afternoon's work")

        result = addons.update_addon("AscensionHonorTracker", entry, self.root)

        self.assertEqual(result.outcome, addons.CHANGED, result.detail)
        kept = self.root / "GnomeWorks.replaced" / "my_edit.lua"
        self.assertTrue(kept.is_file(),
                        "updating one addon destroyed a folder this tool never installed")

    def test_binding_the_whole_repo_says_that_it_holds_several(self):
        # It works, but every addon in the repo will now report an update
        # whenever any one of them changes. Better said once than discovered.
        entry = {"source": "github:o/r", "installed": None, "folders": [], "backup": True}
        result = addons.update_addon("AscensionHonorTracker", entry, self.root)
        notes = " ".join(message for _level, message in result.notes)
        self.assertIn("3 addons", notes)
        self.assertIn("#FolderName", notes)

    def test_bound_to_a_folder_it_touches_nothing_else(self):
        entry = {"source": "github:o/r#AscensionHonorTracker", "installed": None,
                 "folders": [], "backup": True}
        result = addons.update_addon("AscensionHonorTracker", entry, self.root)
        self.assertEqual(result.folders, ["AscensionHonorTracker"])
        self.assertEqual(entry["folders"], ["AscensionHonorTracker"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["AscensionHonorTracker"])


class LayoutsFromRealAscensionAddons(unittest.TestCase):
    """Five links somebody actually handed over, reduced to their shapes.

    Every one of these was checked against the real repository before being
    written down here, because guessing at addon layouts is how the last three
    bugs got in. The names are kept so a failure points at something findable.
    """

    def install(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            return addons.install_zip(mkzip(files), pathlib.Path(tmp), dry_run=True)

    def test_several_addons_that_ship_together(self):
        # gerob/LootCollector: a main addon plus two companions, with bundled
        # libraries nested inside them. The companions install; the libraries
        # must not, or AddOns fills up with LibStub and LibBase64-1.0.
        self.assertEqual(
            self.install({
                "gerob-LootCollector-1a2b3c/LootCollector/LootCollector.toc": "a",
                "gerob-LootCollector-1a2b3c/LootCollector/Libs/LibStub/LibStub.toc": "lib",
                "gerob-LootCollector-1a2b3c/LootCollector/Libs/LibBase64-1.0/LibBase64-1.0.toc": "lib",
                "gerob-LootCollector-1a2b3c/LootCollector_CustomImport/LootCollector_CustomImport.toc": "b",
                "gerob-LootCollector-1a2b3c/LootCollector_StarterDB/LootCollector_StarterDB.toc": "c",
                "gerob-LootCollector-1a2b3c/Docs/notes.md": "d",
            }),
            ["LootCollector", "LootCollector_CustomImport", "LootCollector_StarterDB"],
        )

    def test_one_addon_beside_the_usual_repository_furniture(self):
        # LaSainteChips/AscensionRaidLootCompanion: .github, docs, CHANGELOG,
        # AGENTS.md -- none of which is an addon.
        self.assertEqual(
            self.install({
                "x-1a2b3c/AscensionRaidLootCompanion/AscensionRaidLootCompanion.toc": "a",
                "x-1a2b3c/AscensionRaidLootCompanion/Core/init.lua": "b",
                "x-1a2b3c/docs/guide.md": "c",
                "x-1a2b3c/.github/workflows/ci.yml": "d",
                "x-1a2b3c/CHANGELOG.md": "e",
            }),
            ["AscensionRaidLootCompanion"],
        )

    def test_a_repo_whose_root_is_the_addon_with_one_toc_per_client(self):
        """ayro-CMD/FrostSeek ships seven .toc files, one per WoW flavour.

        They all belong in a single folder named FrostSeek -- the client picks
        the .toc that matches itself. Choosing FrostSeek_Cata.toc would name the
        folder FrostSeek_Cata, which every client would then ignore.
        """
        self.assertEqual(
            self.install({
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek.toc": "a",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_Cata.toc": "b",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_CataPS.toc": "c",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_Mists.toc": "d",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_TBC.toc": "e",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_Vanilla.toc": "f",
                "ayro-CMD-FrostSeek-1a2b3c/FrostSeek_Wrath.toc": "g",
                "ayro-CMD-FrostSeek-1a2b3c/Core.lua": "h",
            }),
            ["FrostSeek"],
        )

    def test_the_addon_is_named_by_its_toc_not_by_the_repository(self):
        """Minnona/Minn-Tinkers holds MinnTinkers.toc -- the hyphen differs.

        The game loads Folder/Folder.toc and silently ignores anything else, so
        installing this as Minn-Tinkers (or as the archive's wrapper name) would
        produce an addon that never appears in the list and no error anywhere.
        """
        self.assertEqual(
            self.install({
                "Minnona-Minn-Tinkers-1a2b3c/MinnTinkers.toc": "a",
                "Minnona-Minn-Tinkers-1a2b3c/Core.lua": "b",
                "Minnona-Minn-Tinkers-1a2b3c/Modules/thing.lua": "c",
            }),
            ["MinnTinkers"],
        )

    def test_a_flavour_toc_does_not_win_when_it_sorts_first(self):
        """The rule is shortest stem, not first alphabetically.

        "-" sorts before ".", so Addon-Classic.toc comes first in the listing
        and would name the folder Addon-Classic -- which no client loads. The
        underscore form does not discriminate here, because "." sorts before
        "_" and the base name wins by accident.
        """
        self.assertEqual(
            self.install({"r-1a2b3c/Addon-Classic.toc": "a", "r-1a2b3c/Addon.toc": "b"}),
            ["Addon"],
        )


class AnAccountIsNotAnAddon(unittest.TestCase):
    """https://github.com/Ascension-Addons names no repository at all.

    It is an easy thing to paste when the addons you want are published by an
    organisation, and "not a GitHub repository" reads as though the link were
    broken rather than as "you are one click short".
    """

    def test_an_account_page_is_recognised_as_one(self):
        for text in ("https://github.com/Ascension-Addons",
                     "https://github.com/Ascension-Addons/",
                     "github.com/Ascension-Addons"):
            with self.subTest(text=text):
                self.assertEqual(addons.github_account(text), "Ascension-Addons")

    def test_a_repository_is_not_mistaken_for_an_account(self):
        for text in ("https://github.com/o/r", "https://github.com/o/r/tree/main/Sub",
                     "o/r", "git@github.com:o/r.git"):
            with self.subTest(text=text):
                self.assertIsNone(addons.github_account(text))

    def test_the_error_says_what_to_do_instead(self):
        with self.assertRaises(addons.Fail) as caught:
            addons.resolve_source("X", "github:https://github.com/Ascension-Addons")
        message = str(caught.exception)
        self.assertIn("Ascension-Addons", message)
        self.assertIn("account", message)
        self.assertIn("Ascension-Addons/repo-name", message)

    def test_it_is_still_refused_rather_than_stored(self):
        # Storing it as a source would produce a 404 at update time, long after
        # the paste, with nothing pointing back at the cause.
        self.assertIsNone(addons.parse_repo("https://github.com/Ascension-Addons"))


class OneAddonBuiltForSeveralClients(unittest.TestCase):
    """Four shapes a "which client?" repository takes, and what each needs.

    Only one of them needs the user to choose. Getting this wrong is quiet:
    an addon built for the wrong client is not something the game reports.
    """

    def install(self, files, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            return addons.install_zip(mkzip(files), pathlib.Path(tmp), dry_run=True, **kw)

    def test_one_toc_per_client_needs_no_choice_at_all(self):
        # The WoW convention, and what ayro-CMD/FrostSeek does. Every flavour
        # .toc belongs in the one folder; the client loads the one that matches
        # itself. Splitting them would break all of them.
        self.assertEqual(
            self.install({
                "r-1a2b/MyAddon.toc": "a", "r-1a2b/MyAddon_Vanilla.toc": "b",
                "r-1a2b/MyAddon_Wrath.toc": "c", "r-1a2b/MyAddon_Cata.toc": "d",
            }),
            ["MyAddon"],
        )

    def test_a_folder_per_client_is_refused_rather_than_guessed(self):
        """Wrath/MyAddon and Retail/MyAddon: sort order must not decide this.

        It did, briefly, and silently: "Retail" sorts before "Wrath", so a repo
        offering both installed the retail build on a Wrath realm with no
        indication that a choice had even been made. Refusing and naming the
        options is the only honest answer -- the tool cannot know which client
        somebody plays.
        """
        files = {"r-1a2b/Wrath/MyAddon/MyAddon.toc": "WRATH",
                 "r-1a2b/Retail/MyAddon/MyAddon.toc": "RETAIL"}
        with self.assertRaises(addons.Fail) as caught:
            self.install(files)
        message = str(caught.exception)
        self.assertIn("Retail", message)
        self.assertIn("Wrath", message)
        self.assertIn("#", message, "the message must say how to choose")

    def test_naming_the_client_folder_installs_that_build(self):
        files = {"r-1a2b/Wrath/MyAddon/MyAddon.toc": "WRATH",
                 "r-1a2b/Retail/MyAddon/MyAddon.toc": "RETAIL"}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            addons.install_zip(mkzip(files), root, False, only="Wrath")
            self.assertEqual((root / "MyAddon" / "MyAddon.toc").read_text(), "WRATH")

    def test_one_way_down_is_still_followed_without_a_choice(self):
        # The refusal must not fire for the ordinary single-addon repo that
        # simply keeps its addon under src/ -- there is nothing to choose.
        self.assertEqual(
            self.install({"r-1a2b/src/MyAddon/MyAddon.toc": "x",
                          "r-1a2b/docs/readme.md": "y"}),
            ["MyAddon"],
        )


class SeveralWoWFolders(unittest.TestCase):
    """One person, a vanilla server, a Wrath one, maybe retail.

    They share nothing. The same addon name means a different addon, possibly
    from a different branch or a different folder of the same repository, and
    certainly a different AddOns directory. Anything that leaks between them is
    a bug that writes files into the wrong game.
    """

    def test_an_install_has_exactly_the_shape_the_manifest_used_to(self):
        # This is what keeps the change small: every function that took the old
        # state and reached for addons or addons_dir now takes one install and
        # is otherwise untouched.
        self.assertEqual(sorted(addons.blank_install()), ["addons", "addons_dir"])

    def test_an_old_manifest_becomes_one_install_named_after_its_folder(self):
        old = {"addons_dir": "/home/me/Games/Ascension/Interface/AddOns",
               "addons": {"Bagnon": {"source": "github:o/r"}}}
        new = addons.migrate(old)
        self.assertEqual(list(new["installs"]), ["Ascension"])
        self.assertEqual(new["current"], "Ascension")
        self.assertEqual(addons.current(new)["addons"]["Bagnon"]["source"], "github:o/r")

    def test_migrating_is_not_repeated_on_an_already_migrated_manifest(self):
        once = addons.migrate({"addons_dir": "/w/Interface/AddOns", "addons": {}})
        self.assertEqual(addons.migrate(once), once)

    def test_a_manifest_that_never_reached_init_gets_no_phantom_install(self):
        # Filing an empty placeholder under a name would leave it in the list
        # forever, and the first real install would arrive as the second entry.
        self.assertEqual(addons.migrate(addons.blank_install())["installs"], {})

    def test_the_placeholder_is_swept_up_when_a_real_one_arrives(self):
        state = {"installs": {"default": addons.blank_install()}, "current": "default"}
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        self.assertEqual(list(state["installs"]), ["Wrath"])

    def test_naming_an_install_after_its_folder_skips_interface_and_addons(self):
        for directory, expected in [
            ("/games/Ascension/Interface/AddOns", "Ascension"),
            ("/games/Vanilla/Interface/addons", "Vanilla"),
            ("/games/Wrath", "Wrath"),
        ]:
            with self.subTest(directory=directory):
                self.assertEqual(addons.install_name_for(directory), expected)

    def test_two_installs_of_the_same_name_do_not_collide(self):
        self.assertEqual(
            addons.install_name_for("/elsewhere/Wrath/Interface/AddOns", taken={"Wrath"}),
            "Wrath (2)",
        )

    def test_the_same_addon_can_be_bound_differently_in_each(self):
        """The whole point of this. A shared addon name, two different sources.

        Ascension is a Wrath-based realm; another server may be vanilla. The
        same repository can hold a build for each, and binding one must say
        nothing about the other.
        """
        state = {"installs": {}}
        addons.add_install(state, pathlib.Path("/games/Vanilla/Interface/AddOns"))
        addons.set_source(addons.current(state), "Shared", "github:o/r#Vanilla")
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        addons.set_source(addons.current(state), "Shared", "github:o/r#Wrath")

        self.assertEqual(addons.pick(state, "Vanilla")["addons"]["Shared"]["source"],
                         "github:o/r#Vanilla")
        self.assertEqual(addons.pick(state, "Wrath")["addons"]["Shared"]["source"],
                         "github:o/r#Wrath")

    def test_pick_does_not_change_which_install_is_current(self):
        # --install aims one run elsewhere. It would be a nasty surprise if it
        # quietly left every later command pointed at the other game.
        state = {"installs": {}}
        addons.add_install(state, pathlib.Path("/games/Vanilla/Interface/AddOns"))
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        addons.pick(state, "Vanilla")
        self.assertEqual(addons.current_name(state), "Wrath")

    def test_use_does_change_it(self):
        state = {"installs": {}}
        addons.add_install(state, pathlib.Path("/games/Vanilla/Interface/AddOns"))
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        addons.use(state, "Vanilla")
        self.assertEqual(addons.current_name(state), "Vanilla")

    def test_an_unknown_install_is_refused_with_the_list(self):
        state = {"installs": {}}
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        with self.assertRaises(addons.Fail) as caught:
            addons.pick(state, "Typo")
        self.assertIn("Wrath", str(caught.exception))

    def test_pointing_init_at_the_same_folder_twice_is_an_update(self):
        # Re-running init after moving the game must not leave two entries
        # racing to manage one directory.
        state = {"installs": {}}
        first = addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        again = addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        self.assertEqual(first, again)
        self.assertEqual(len(state["installs"]), 1)

    def test_forgetting_one_moves_current_somewhere_real(self):
        state = {"installs": {}}
        addons.add_install(state, pathlib.Path("/games/Vanilla/Interface/AddOns"))
        addons.add_install(state, pathlib.Path("/games/Wrath/Interface/AddOns"))
        addons.forget_install(state, "Wrath")
        self.assertEqual(addons.current_name(state), "Vanilla")
        self.assertNotIn("Wrath", state["installs"])

    def test_current_is_stable_when_the_record_is_unclear(self):
        # An arbitrary answer here would update a different WoW folder than the
        # last run did, which is how files land in the wrong game.
        state = {"installs": {"Wrath": addons.blank_install(),
                              "Vanilla": addons.blank_install()},
                 "current": "gone"}
        self.assertEqual(addons.current_name(state), "Vanilla")
        self.assertEqual(addons.current_name(state), "Vanilla")

    def test_handing_the_whole_manifest_to_an_install_function_is_an_error(self):
        """An install and the old manifest are the same shape, so this is easy.

        Quiet, too: setdefault("addons", {}) on a manifest writes the binding
        into a top-level key nothing reads, and the addon looks bound and never
        updates. A TypeError stops a test dead instead.
        """
        state = addons.migrate(addons.blank_install())
        for call in (
            lambda: addons.set_source(state, "A", "unmanaged"),
            lambda: addons.accept_suggestions(state),
            lambda: addons.addons_dir(state),
            lambda: addons.rescan(state, pathlib.Path(".")),
        ):
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()


class OfferingWhatARepositoryHolds(unittest.TestCase):
    """The list somebody ticks from, and the rule behind it.

    A folder counts as an addon when it holds a .toc named after itself -- the
    same rule the installer applies to an archive, so the list cannot offer
    something the install would then fail to find.
    """

    def setUp(self):
        self._real = addons.http_json
        self.addCleanup(lambda: setattr(addons, "http_json", self._real))

    def serve(self, paths, truncated=False):
        blobs = [{"path": p, "type": "blob"} for p in paths]
        trees = [{"path": d, "type": "tree"} for d in sorted(
            {"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)})]
        addons.http_json = lambda url: (
            {"tree": blobs + trees, "truncated": truncated} if "git/trees" in url
            else {"default_branch": "main"}
        )

    def test_a_repository_of_several_addons(self):
        self.serve([
            "AscensionHonorTracker/AscensionHonorTracker.toc",
            "GnomeWorks/GnomeWorks.toc",
            "TurboPlates/TurboPlates.toc",
            "management/notes.md",
            "README.md",
        ])
        self.assertEqual(addons.addons_in_repo("o/r"),
                         ["AscensionHonorTracker", "GnomeWorks", "TurboPlates"])

    def test_bundled_libraries_are_not_offered(self):
        # LootCollector ships LibStub and LibBase64-1.0 inside itself. Each is
        # an addon by the letter of the rule and nobody choosing means them.
        self.serve([
            "LootCollector/LootCollector.toc",
            "LootCollector/Libs/LibStub/LibStub.toc",
            "LootCollector/Libs/LibBase64-1.0/LibBase64-1.0.toc",
        ])
        self.assertEqual(addons.addons_in_repo("o/r"), ["LootCollector"])

    def test_a_folder_whose_toc_does_not_match_is_not_an_addon(self):
        # The client loads Folder/Folder.toc and ignores anything else, so a
        # mismatch is not an addon however much it looks like one.
        self.serve(["Something/Different.toc"])
        self.assertEqual(addons.addons_in_repo("o/r"), [])

    def test_a_repo_whose_root_is_the_addon_offers_nothing(self):
        # FrostSeek, Minn-Tinkers. There is no choice to make.
        self.serve(["FrostSeek.toc", "FrostSeek_Wrath.toc", "Core.lua"])
        self.assertEqual(addons.addons_in_repo("o/r"), [])

    def test_an_addon_one_level_down_is_offered(self):
        self.serve(["src/MyAddon/MyAddon.toc", "docs/readme.md"])
        self.assertEqual(addons.addons_in_repo("o/r"), ["src/MyAddon"])

    def test_a_repository_too_large_to_list_says_so(self):
        # Better than an empty list, which reads as "no addons in here".
        self.serve(["docs/readme.md"], truncated=True)
        with self.assertRaises(addons.Fail) as caught:
            addons.addons_in_repo("o/r")
        self.assertIn("too large", str(caught.exception))

    def test_the_likely_one_is_the_addons_own_name(self):
        folders = ["Alpha", "GnomeWorks", "Zeta"]
        self.assertEqual(addons.likely_addon("GnomeWorks", folders), "GnomeWorks")
        self.assertEqual(addons.likely_addon("gnomeworks", folders), "GnomeWorks")

    def test_no_guess_at_all_when_nothing_matches(self):
        """A wrong guess that arrives pre-ticked is worse than no guess.

        It gets accepted without being read, and the addon then updates from
        somebody else's folder -- silently, because both are real addons.
        """
        self.assertIsNone(addons.likely_addon("Bagnon", ["Alpha", "Beta"]))
        self.assertIsNone(addons.likely_addon("Anything", []))


class SeveralFoldersInOneRow(unittest.TestCase):
    """A main addon and its companion are one thing to whoever updates them."""

    def test_a_source_can_name_more_than_one(self):
        self.assertEqual(addons.wanted_folders("Main,Main_Companion"),
                         ["Main", "Main_Companion"])
        self.assertEqual(addons.wanted_folders(" Main , Sub/Deep , "),
                         ["Main", "Sub/Deep"])
        self.assertEqual(addons.wanted_folders(""), [])
        self.assertEqual(addons.wanted_folders(None), [])

    def test_only_the_named_folders_are_installed(self):
        archive = mkzip({
            "r-1a2b/Main/Main.toc": "a",
            "r-1a2b/Main_Companion/Main_Companion.toc": "b",
            "r-1a2b/Unrelated/Unrelated.toc": "c",
        })
        with tempfile.TemporaryDirectory() as tmp:
            written = addons.install_zip(archive, pathlib.Path(tmp), dry_run=True,
                                         only="Main,Main_Companion")
        self.assertEqual(sorted(written), ["Main", "Main_Companion"])

    def test_the_version_moves_when_either_folder_moves(self):
        responses = {}
        real = addons.http_json
        addons.http_json = lambda url: responses.get(url)
        self.addCleanup(lambda: setattr(addons, "http_json", real))
        repo = "https://api.github.com/repos/o/r"
        responses[repo] = {"default_branch": "main"}
        responses[f"{repo}/commits?sha=main&path=Main&per_page=1"] = [{"sha": "aaaaaaaaaaaa"}]
        responses[f"{repo}/commits?sha=main&path=Sub&per_page=1"] = [{"sha": "bbbbbbbbbbbb"}]
        first = addons.latest_github("o/r#Main,Sub")[0]

        # Only the SECOND folder changes. Were the version taken from the first
        # alone, ticking a second folder would quietly stop it ever updating.
        responses[f"{repo}/commits?sha=main&path=Sub&per_page=1"] = [{"sha": "cccccccccccc"}]
        self.assertNotEqual(addons.latest_github("o/r#Main,Sub")[0], first)


class FlaggingAWholeRepositoryBinding(unittest.TestCase):
    def test_a_row_that_installs_several_addons_is_flagged(self):
        entry = {"source": "github:o/r", "folders": ["A", "B", "C"], "installed": "v1"}
        self.assertTrue(addons.covers_several_addons(entry))

    def test_a_row_bound_to_one_folder_is_not(self):
        entry = {"source": "github:o/r#A", "folders": ["A"], "installed": "v1"}
        self.assertFalse(addons.covers_several_addons(entry))

    def test_an_ordinary_single_addon_repo_is_not(self):
        entry = {"source": "github:o/r", "folders": ["OnlyOne"], "installed": "v1"}
        self.assertFalse(addons.covers_several_addons(entry))

    def test_nothing_is_claimed_before_the_first_install(self):
        # Whether a github: source holds one addon or nine is unknowable until
        # the archive is unpacked, so an unflagged row is honest, not a miss.
        self.assertFalse(addons.covers_several_addons({"source": "github:o/r"}))

    def test_local_and_unmanaged_rows_are_never_flagged(self):
        self.assertFalse(addons.covers_several_addons(
            {"source": "local:/x", "folders": ["A", "B"], "installed": "linked"}))
        self.assertFalse(addons.covers_several_addons({"source": "unmanaged"}))


class FakeResponse:
    """What urlopen returns, reduced to the three things http_json touches."""

    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        # Real responses take a size, and the engine passes one when it wants
        # to cap how much of a ref advertisement it will read. A double that
        # refuses the argument turns that into a silent fall back to the API.
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class PacingGitHub(unittest.TestCase):
    """The quota is small and the burst limit is real; both must survive a run.

    The regression these pin: `Update all` over a normal addon list fired every
    request it needed as fast as the network answered, which is how a list that
    fits inside sixty calls an hour still came back "GitHub rate limit reached"
    -- and then spent one doomed round trip per remaining addon saying so again.
    """

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self._config = addons.CONFIG_DIR
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name)
        self.addCleanup(lambda: setattr(addons, "CONFIG_DIR", self._config))

        # A throttle that reports its sleeps rather than taking them, so the
        # pacing can be asserted without the suite sitting through it.
        self.slept = []
        self.now = [1_000_000.0]
        self.real_throttle = addons.THROTTLE
        addons.THROTTLE = addons.Throttle(sleep=self.sleep, clock=lambda: self.now[0])
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.addCleanup(addons.set_wait_hook, None)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now[0] += seconds

    def urlopen(self, answers):
        """Stub urlopen with a scripted list; records the URLs asked for."""
        self.asked = []
        queue = list(answers)

        def fake(request, timeout=0):
            self.asked.append(request.full_url)
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    def limit(self, headers: dict, message: str = "API rate limit exceeded"):
        body = json.dumps({"message": message}).encode()
        return urllib.error.HTTPError("http://x", 403, "Forbidden", headers, io.BytesIO(body))

    # -- spacing -------------------------------------------------------------

    def test_the_first_call_waits_for_nothing(self):
        addons.THROTTLE.wait_turn()
        self.assertEqual(self.slept, [])

    def test_two_calls_in_a_row_are_spaced_out(self):
        addons.THROTTLE.wait_turn()
        addons.THROTTLE.wait_turn()
        self.assertEqual(self.slept, [addons.GITHUB_MIN_GAP])

    def test_plenty_of_quota_keeps_the_gap_at_its_floor(self):
        addons.THROTTLE.observe({"x-ratelimit-remaining": "4999",
                                 "x-ratelimit-reset": str(self.now[0] + 3600)})
        self.assertEqual(addons.THROTTLE.gap(), addons.GITHUB_MIN_GAP)

    def test_a_nearly_spent_quota_is_spread_over_the_time_it_has_left(self):
        # Ten calls left and twenty seconds until they come back: two seconds
        # each, rather than ten in the same second and nothing afterwards.
        addons.THROTTLE.observe({"x-ratelimit-remaining": "10",
                                 "x-ratelimit-reset": str(self.now[0] + 20)})
        self.assertEqual(addons.THROTTLE.gap(), 2.0)

    def test_the_gap_is_capped_so_a_run_never_looks_hung(self):
        addons.THROTTLE.observe({"x-ratelimit-remaining": "1",
                                 "x-ratelimit-reset": str(self.now[0] + 3600)})
        self.assertEqual(addons.THROTTLE.gap(), addons.GITHUB_MAX_GAP)

    def test_a_wait_worth_noticing_is_announced(self):
        said = []
        addons.set_wait_hook(lambda seconds, why: said.append((seconds, why)))
        addons.THROTTLE.observe({"x-ratelimit-remaining": "1",
                                 "x-ratelimit-reset": str(self.now[0] + 3600)})
        addons.THROTTLE.wait_turn()
        addons.THROTTLE.wait_turn()
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0][0], addons.GITHUB_MAX_GAP)

    def test_a_quarter_second_is_not_worth_announcing(self):
        said = []
        addons.set_wait_hook(lambda seconds, why: said.append(seconds))
        addons.THROTTLE.wait_turn()
        addons.THROTTLE.wait_turn()
        self.assertEqual(said, [])

    # -- not spending calls twice --------------------------------------------

    def test_the_same_question_is_only_asked_once(self):
        # Two addons out of one repository ask for the same default branch. The
        # second answer is already in hand; buying it again costs quota that
        # the addon after them then does not have.
        self.urlopen([FakeResponse(b'{"default_branch": "main"}',
                                   {"x-ratelimit-remaining": "58"})])
        url = "https://api.github.com/repos/o/r"
        self.assertEqual(addons.http_json(url), {"default_branch": "main"})
        self.assertEqual(addons.http_json(url), {"default_branch": "main"})
        self.assertEqual(len(self.asked), 1)

    def test_a_404_is_remembered_as_an_answer(self):
        # "This repo publishes no releases" is asked once per addon and is a
        # perfectly good thing to know without asking twice.
        missing = urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"{}"))
        self.urlopen([missing])
        url = "https://api.github.com/repos/o/r/releases/latest"
        self.assertIsNone(addons.http_json(url))
        self.assertIsNone(addons.http_json(url))
        self.assertEqual(len(self.asked), 1)


    # -- hitting the wall ----------------------------------------------------

    def test_an_exhausted_quota_is_not_asked_again(self):
        # The point: one failure, not one per remaining addon. Forty more round
        # trips cannot produce a different answer before the reset.
        reset = self.now[0] + 1800
        self.urlopen([self.limit({"x-ratelimit-remaining": "0",
                                  "x-ratelimit-reset": str(reset)})])
        for url in ("https://api.github.com/repos/o/one", "https://api.github.com/repos/o/two"):
            with self.assertRaises(addons.Fail) as caught:
                addons.http_json(url)
            self.assertIn("rate limit", str(caught.exception).lower())
        self.assertEqual(len(self.asked), 1)

    def test_the_message_says_when_the_quota_comes_back(self):
        reset = self.now[0] + 1800
        expected = datetime.datetime.fromtimestamp(reset).strftime("%H:%M")
        self.assertIn(expected, addons.rate_limit_message(reset))

    def test_a_quota_that_has_come_back_is_asked_again(self):
        addons.THROTTLE.observe({"x-ratelimit-remaining": "0",
                                 "x-ratelimit-reset": str(self.now[0] + 10)})
        self.assertTrue(addons.THROTTLE.spent())
        self.now[0] += 11
        self.assertFalse(addons.THROTTLE.spent())

    def test_a_burst_limit_is_waited_out_and_retried(self):
        # A secondary limit arrives with quota still on the clock and clears in
        # about a minute, so waiting is the whole fix -- and failing the addon
        # instead would be failing it for being one of several in a row.
        burst = self.limit({"retry-after": "5", "x-ratelimit-remaining": "42"},
                           "You have exceeded a secondary rate limit")
        self.urlopen([burst, FakeResponse(b'{"default_branch": "main"}', {})])
        self.assertEqual(addons.http_json("https://api.github.com/repos/o/r"),
                         {"default_branch": "main"})
        self.assertIn(5.0, self.slept)
        self.assertEqual(len(self.asked), 2)

    def test_an_hour_long_limit_is_not_waited_out(self):
        # Sitting in front of a frozen window for forty minutes is worse than
        # being told to come back later.
        self.urlopen([self.limit({"x-ratelimit-remaining": "0",
                                  "x-ratelimit-reset": str(self.now[0] + 2400)})])
        with self.assertRaises(addons.Fail):
            addons.http_json("https://api.github.com/repos/o/r")
        self.assertEqual(self.slept, [])
        self.assertEqual(len(self.asked), 1)

    def test_a_forbidden_with_quota_left_still_says_what_github_said(self):
        # The pacing must not swallow the case it was never about.
        self.urlopen([self.limit({"x-ratelimit-remaining": "4999"},
                                 "GitHub access is not enabled for this session.")])
        with self.assertRaises(addons.Fail) as caught:
            addons.http_json("https://api.github.com/repos/o/r")
        self.assertIn("not enabled for this session", str(caught.exception))
        self.assertNotIn("rate limit", str(caught.exception).lower())


    # -- archives count too --------------------------------------------------

    def test_a_zipball_is_paced_like_any_other_call(self):
        # It comes out of the same hourly quota as the version check that
        # found it, so pacing the checks and not the downloads would leave the
        # bigger half of a thirty-addon update unaccounted for.
        self.urlopen([FakeResponse(b"PK\x03\x04", {"x-ratelimit-remaining": "40"})])
        addons.download("https://api.github.com/repos/o/r/zipball/main")
        addons.download("https://api.github.com/repos/o/r/zipball/main")
        self.assertEqual(self.slept, [addons.GITHUB_MIN_GAP])

    def test_a_release_asset_is_fetched_at_full_speed(self):
        # Served from GitHub's downloads host, not the API: no quota, no pause.
        self.urlopen([FakeResponse(b"PK\x03\x04")])
        addons.download("https://github.com/o/r/releases/download/v1/MyAddon.zip")
        addons.download("https://github.com/o/r/releases/download/v1/MyAddon.zip")
        self.assertEqual(self.slept, [])

    def test_an_exhausted_quota_stops_the_downloads_too(self):
        addons.THROTTLE.observe({"x-ratelimit-remaining": "0",
                                 "x-ratelimit-reset": str(self.now[0] + 1800)})
        self.urlopen([FakeResponse(b"PK\x03\x04")])
        with self.assertRaises(addons.Fail) as caught:
            addons.download("https://api.github.com/repos/o/r/zipball/main")
        self.assertIn("rate limit", str(caught.exception).lower())
        self.assertEqual(self.asked, [])


class BoundAddonsComeFirst(unittest.TestCase):
    """The list is read to find the addons this tool maintains, so those lead.

    On a real install most rows are unmanaged, and alphabetical order buries
    the six that matter among fifty that do not.
    """

    def order(self, **entries):
        return addons.display_order(entries)

    def test_sourced_addons_lead_the_list(self):
        self.assertEqual(
            self.order(
                Zulu={"source": "github:o/r"},
                Alpha={"source": "unmanaged"},
                Bravo={"source": "local:/somewhere"},
            ),
            ["Bravo", "Zulu", "Alpha"],
        )

    def test_each_group_is_still_alphabetical(self):
        self.assertEqual(
            self.order(
                delta={"source": "github:o/d"},
                Charlie={"source": "github:o/c"},
                zulu={"source": "unmanaged"},
                Alpha={"source": "unmanaged"},
            ),
            ["Charlie", "delta", "Alpha", "zulu"],
        )

    def test_a_suggestion_is_not_a_source(self):
        # A .toc header is the author's claim about where the code lives, not
        # this user's decision to install from there -- the row is still loose.
        self.assertEqual(
            self.order(
                Bound={"source": "github:o/r"},
                Suggested={"source": "unmanaged", "suggested": "github:o/s"},
            ),
            ["Bound", "Suggested"],
        )

    def test_an_entry_with_no_source_field_counts_as_loose(self):
        self.assertEqual(
            self.order(Aaa={}, Zzz={"source": "github:o/r"}),
            ["Zzz", "Aaa"],
        )

    def test_an_empty_list_is_empty(self):
        self.assertEqual(self.order(), [])


class AskingForFree(unittest.TestCase):
    """A check that finds nothing new should cost nothing.

    GitHub does not bill a 304 against the hourly quota, so an ETag turns
    "you may check twice an hour" into "check as often as you like, as long as
    your addons are not moving". It is also fresher than the timed cache it
    replaced, which reported a version two minutes out of date -- exactly wrong
    for somebody pushing a change and immediately updating to test it.
    """

    URL = "https://api.github.com/repos/o/r"

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self._config = addons.CONFIG_DIR
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name)
        self.real_throttle = addons.THROTTLE
        addons.THROTTLE = addons.Throttle(sleep=lambda _s: None, clock=lambda: 0.0)
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.addCleanup(lambda: setattr(addons, "CONFIG_DIR", self._config))

        self.body = {"default_branch": "main"}
        self.etag = 'W/"one"'
        self.conditional_on = []
        self.served = []

        def fake(request, timeout=0):
            asked = request.get_header("If-none-match")
            self.conditional_on.append(asked)
            if asked == self.etag:
                self.served.append(304)
                raise urllib.error.HTTPError(
                    request.full_url, 304, "Not Modified",
                    {"etag": self.etag, "x-ratelimit-remaining": "57"}, io.BytesIO(b""),
                )
            self.served.append(200)
            return FakeResponse(json.dumps(self.body).encode(),
                                {"etag": self.etag, "x-ratelimit-remaining": "56"})

        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    def test_the_first_look_costs_a_call_and_the_next_run_does_not(self):
        addons.begin_run()
        self.assertEqual(addons.http_json(self.URL), {"default_branch": "main"})
        addons.begin_run()
        self.assertEqual(addons.http_json(self.URL), {"default_branch": "main"})
        # Asked GitHub both times -- the second answer is as fresh as the first.
        self.assertEqual(self.served, [200, 304])
        self.assertEqual(self.conditional_on, [None, self.etag])

    def test_a_new_answer_is_seen_the_moment_it_changes(self):
        # The regression the timed cache introduced: push, update, and be told
        # you are up to date because the answer was two minutes old.
        addons.begin_run()
        addons.http_json(self.URL)
        self.body = {"default_branch": "develop"}
        self.etag = 'W/"two"'
        addons.begin_run()
        self.assertEqual(addons.http_json(self.URL), {"default_branch": "develop"})

    def test_one_run_asks_once_however_many_addons_want_it(self):
        # Ten rows out of one repository, one branch lookup. A branch cannot
        # move between two rows of the same pass.
        addons.begin_run()
        for _ in range(10):
            addons.http_json(self.URL)
        self.assertEqual(self.served, [200])

    def test_what_was_learned_survives_the_process(self):
        addons.begin_run()
        addons.http_json(self.URL)
        addons.end_run()
        self.assertTrue(addons.cache_path().is_file())

        addons.forget_github_state()          # as if the app had been restarted
        addons.begin_run()
        self.assertEqual(addons.http_json(self.URL), {"default_branch": "main"})
        self.assertEqual(self.served, [200, 304])

    def test_a_404_is_not_remembered_between_runs(self):
        # "This repo publishes no releases" is true until the day it is not,
        # and a 404 carries no ETag to find that out with.
        missing = urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"{}"))
        def fake(request, timeout=0):
            self.served.append(404)
            raise missing
        addons.urllib.request.urlopen = fake

        addons.begin_run()
        self.assertIsNone(addons.http_json(self.URL))
        self.assertIsNone(addons.http_json(self.URL))
        addons.begin_run()
        self.assertIsNone(addons.http_json(self.URL))
        self.assertEqual(self.served, [404, 404])

    def test_an_unwritable_config_directory_does_not_fail_the_run(self):
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name) / "nope.txt"
        addons.CONFIG_DIR.parent.mkdir(parents=True, exist_ok=True)
        (pathlib.Path(self.scratch.name) / "nope.txt").write_text("not a directory")
        addons.begin_run()
        addons.http_json(self.URL)
        addons.end_run()          # must not raise


class ArchivesOffTheMeter(unittest.TestCase):
    """The download half of an update need not be spent out of the API quota.

    `api.github.com/repos/o/r/zipball/ref` is a REST call and is billed as one.
    codeload is the host behind the "Download ZIP" button: same bytes, no quota.
    It is undocumented, so the REST URL stays as a fallback -- a run that
    installs the addon and spends a call beats a run that fails for free.
    """

    def setUp(self):
        self.real_throttle = addons.THROTTLE
        self.slept = []
        addons.THROTTLE = addons.Throttle(sleep=self.slept.append, clock=lambda: 0.0)
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.asked = []

    def serve(self, fail_codeload=False):
        def fake(request, timeout=0):
            url = request.full_url
            self.asked.append(url)
            if fail_codeload and "codeload.github.com" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
            return FakeResponse(b"PK\x03\x04", {"x-ratelimit-remaining": "50"})
        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    def test_a_branch_archive_goes_to_codeload(self):
        self.assertEqual(addons.archive_url("o/r", "main"),
                         "https://codeload.github.com/o/r/zip/refs/heads/main")

    def test_a_tag_archive_goes_to_codeload(self):
        self.assertEqual(addons.archive_url("o/r", "v1.2", tag=True),
                         "https://codeload.github.com/o/r/zip/refs/tags/v1.2")

    def test_a_branch_with_a_slash_survives_the_url(self):
        self.assertEqual(addons.archive_url("o/r", "feature/new"),
                         "https://codeload.github.com/o/r/zip/refs/heads/feature/new")

    def test_codeload_costs_no_quota_and_no_pause(self):
        self.serve()
        addons.download(addons.archive_url("o/r", "main"))
        addons.download(addons.archive_url("o/r", "main"))
        self.assertEqual(self.slept, [])

    def test_a_refusal_falls_back_to_the_rest_archive(self):
        self.serve(fail_codeload=True)
        self.assertEqual(addons.download(addons.archive_url("o/r", "main")), b"PK\x03\x04")
        self.assertEqual(self.asked, [
            "https://codeload.github.com/o/r/zip/refs/heads/main",
            "https://api.github.com/repos/o/r/zipball/main",
        ])

    def test_a_tag_falls_back_to_the_right_ref(self):
        self.serve(fail_codeload=True)
        addons.download(addons.archive_url("o/r", "v1.2", tag=True))
        self.assertEqual(self.asked[-1], "https://api.github.com/repos/o/r/zipball/v1.2")

    def test_a_release_asset_has_no_fallback_to_invent(self):
        # Not a codeload URL, so a failure is a real failure and is reported.
        self.serve()
        addons.urllib.request.urlopen = lambda request, timeout=0: (_ for _ in ()).throw(
            urllib.error.HTTPError(request.full_url, 500, "Boom", {}, io.BytesIO(b""))
        )
        with self.assertRaises(urllib.error.HTTPError):
            addons.download("https://github.com/o/r/releases/download/v1/A.zip")


class ARepositoryWithoutReleases(unittest.TestCase):
    """"This repo publishes no releases" must not cost a call every time.

    The regression, found on the first real run of v0.7.0: a warm "Check for
    updates" dropped the hourly quota by exactly the number of addons bound to
    repositories that have never cut a release. `/releases/latest` answers 404
    for those, and a 404 carries no ETag, so it was the one question that could
    never be revalidated -- six such addons cost six calls an hour, for ever,
    to be told six times what had not changed.

    Listing answers 200 and an empty array, which does carry an ETag.
    """

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self._config = addons.CONFIG_DIR
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name)
        self.real_throttle = addons.THROTTLE
        addons.THROTTLE = addons.Throttle(sleep=lambda _s: None, clock=lambda: 0.0)
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.addCleanup(lambda: setattr(addons, "CONFIG_DIR", self._config))

        self.releases = []
        self.billed = []

        def fake(request, timeout=0):
            url = request.full_url
            tail = url.split("/repos/")[1]
            if tail.endswith("/releases?per_page=1"):
                body = json.dumps(self.releases[:1]).encode()
            elif tail.endswith("/releases/latest"):
                stable = [r for r in self.releases
                          if not r.get("draft") and not r.get("prerelease")]
                if not stable:
                    self.billed.append("404 " + tail)
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))
                body = json.dumps(stable[0]).encode()
            elif tail.endswith("/r"):
                body = b'{"default_branch": "main"}'
            else:
                body = json.dumps({"sha": "abc123def456"}).encode()
            tag = f'W/"{abs(hash((url, json.dumps(self.releases)))) & 0xffff:x}"'
            if request.get_header("If-none-match") == tag:
                raise urllib.error.HTTPError(url, 304, "Not Modified", {"etag": tag}, io.BytesIO(b""))
            self.billed.append("200 " + tail)
            return FakeResponse(body, {"etag": tag, "x-ratelimit-remaining": "50"})

        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    def check(self):
        addons.begin_run()
        found = addons.latest_github("o/r")
        addons.end_run()
        return found

    def test_a_second_check_costs_nothing(self):
        self.check()
        self.billed.clear()
        self.check()
        self.assertEqual(self.billed, [])

    def test_it_still_tracks_the_branch_head(self):
        version, url = self.check()
        self.assertEqual(version, "abc123def456")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/main")

    def test_a_first_release_is_seen_on_the_very_next_check(self):
        # No timer and nothing taken on trust: the whole reason for listing
        # rather than remembering the 404.
        self.check()
        self.releases = [{"tag_name": "v1.0", "assets": []}]
        version, url = self.check()
        self.assertEqual(version, "v1.0")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/tags/v1.0")

    def test_the_newest_release_answers_outright(self):
        self.releases = [{"tag_name": "v3.0", "assets": []}]
        self.check()
        # One call for the listing; the narrower endpoint is not needed at all.
        self.assertNotIn("200 o/r/releases/latest", self.billed)

    def test_a_pre_release_is_not_installed_over_the_stable_one(self):
        # The rule /releases/latest exists for. Listing alone would hand back
        # the pre-release, which is not what somebody updating an addon wants.
        self.releases = [
            {"tag_name": "v4.0-beta", "prerelease": True, "assets": []},
            {"tag_name": "v3.0", "assets": []},
        ]
        self.assertEqual(self.check()[0], "v3.0")

    def test_a_draft_is_not_installed_either(self):
        self.releases = [
            {"tag_name": "v4.0", "draft": True, "assets": []},
            {"tag_name": "v3.0", "assets": []},
        ]
        self.assertEqual(self.check()[0], "v3.0")

    def test_a_repo_whose_only_release_is_a_pre_release_falls_to_the_branch(self):
        self.releases = [{"tag_name": "v0.1-rc1", "prerelease": True, "assets": []}]
        self.assertEqual(self.check()[0], "abc123def456")


def advertisement(head: str, refs: dict) -> bytes:
    """A git ref advertisement, in the pkt-line format a server really sends."""
    def pkt(text: str) -> bytes:
        line = text.encode()
        return b"%04x" % (len(line) + 4) + line

    out = pkt("# service=git-upload-pack\n") + b"0000"
    first = True
    for name, sha in refs.items():
        if first:
            caps = f"multi_ack symref=HEAD:refs/heads/{head} agent=git/github"
            out += pkt(f"{sha} {name}\x00{caps}\n")
            first = False
        else:
            out += pkt(f"{sha} {name}\n")
    return out + b"0000"


class AskingGitInsteadOfTheAPI(unittest.TestCase):
    """The ref advertisement is free; most of what we asked the API is in it.

    `github.com/o/r.git/info/refs?service=git-upload-pack` is the request every
    clone begins with. It is not the REST API, carries no x-ratelimit headers
    and is not billed against the hourly quota -- and it answers, for a whole
    repository at once, the default branch, every branch head and every tag.
    """

    HEADS = {
        "refs/heads/main": "1111111111111111111111111111111111111111",
        "refs/heads/dev": "2222222222222222222222222222222222222222",
    }

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self._config = addons.CONFIG_DIR
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name)
        self.real_throttle = addons.THROTTLE
        addons.THROTTLE = addons.Throttle(sleep=lambda _s: None, clock=lambda: 0.0)
        # These tests are about the shortcut, so the module-wide stub comes off.
        self.stub = addons.git_refs
        addons.git_refs = _real_git_refs
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "git_refs", self.stub))
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.addCleanup(lambda: setattr(addons, "CONFIG_DIR", self._config))

        self.refs = dict(self.HEADS)
        self.api = []
        self.git = []

        def fake(request, timeout=0):
            url = request.full_url
            if "info/refs" in url:
                self.git.append(url)
                return FakeResponse(advertisement("main", self.refs))
            self.api.append(url.split("/repos/")[1])
            tail = url
            if tail.endswith("/releases?per_page=1"): body = b"[]"
            elif "/commits?" in tail: body = json.dumps([{"sha": "f0" * 20}]).encode()
            elif tail.rstrip("/").endswith("o/r"): body = b'{"default_branch": "main"}'
            else: body = json.dumps({"sha": "f0" * 20}).encode()
            return FakeResponse(body, {"etag": 'W/"x"', "x-ratelimit-remaining": "50"})

        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    def check(self, source="o/r"):
        addons.begin_run()
        found = addons.latest_github(source)
        addons.end_run()
        return found

    # -- reading the advertisement -------------------------------------------

    def test_the_default_branch_is_advertised_not_asked_for(self):
        self.assertEqual(addons.default_branch("o/r"), "main")
        self.assertEqual(self.api, [])

    def test_a_branch_head_is_advertised_not_asked_for(self):
        version, url = self.check("o/r@dev")
        self.assertEqual(version, "222222222222")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/heads/dev")
        self.assertEqual(self.api, [])

    def test_pull_request_refs_are_not_kept(self):
        # GitHub advertises every PR as refs/pull/N/head. On a busy repository
        # that is most of the response and none of the answer.
        self.refs["refs/pull/7/head"] = "9" * 40
        addons.begin_run()
        self.assertNotIn("refs/pull/7/head", addons.git_refs("o/r")["refs"])

    def test_a_peeled_tag_does_not_shadow_the_tag(self):
        self.refs["refs/tags/v1"] = "3" * 40
        self.refs["refs/tags/v1^{}"] = "4" * 40
        addons.begin_run()
        self.assertEqual(addons.git_refs("o/r")["refs"]["refs/tags/v1"], "3" * 40)
        self.assertNotIn("refs/tags/v1^{}", addons.git_refs("o/r")["refs"])

    # -- what it lets us not ask ---------------------------------------------

    def test_a_repo_with_no_tags_never_asks_about_releases(self):
        # A published release always has a tag behind it, so no tags is a
        # complete answer -- and the whole cost of the binding shape that used
        # to pay for a 404 on every single run.
        self.check()
        self.assertEqual(self.api, [])

    def test_a_repo_with_tags_still_asks(self):
        self.refs["refs/tags/v1"] = "3" * 40
        self.check()
        self.assertTrue(any("releases" in call for call in self.api))

    def test_an_unmoved_branch_costs_nothing_for_a_folder(self):
        # The monorepo case: ten folders on a branch that has not moved used to
        # be ten history queries.
        self.check("o/r#Alpha")
        self.assertEqual(self.api, ["o/r/commits?sha=main&path=Alpha&per_page=1"])
        self.api.clear()
        self.check("o/r#Alpha")
        self.assertEqual(self.api, [])

    def test_a_moved_branch_asks_again(self):
        # And it must, or the addon silently stops updating.
        self.check("o/r#Alpha")
        self.api.clear()
        self.refs["refs/heads/main"] = "5" * 40
        self.check("o/r#Alpha")
        self.assertEqual(self.api, ["o/r/commits?sha=main&path=Alpha&per_page=1"])

    def test_ten_folders_share_one_advertisement(self):
        addons.begin_run()
        for i in range(10):
            addons.latest_github(f"o/r#Addon{i}")
        addons.end_run()
        self.assertEqual(len(self.git), 1)

    def test_a_tag_pinned_source_gets_the_tag_archive_path(self):
        # `@v1.0` is a tag, and a tag archive does not live under refs/heads.
        # Getting it wrong is not fatal -- codeload refuses and the REST
        # zipball serves it -- but it is a wasted round trip on every install.
        self.refs["refs/tags/v1.0"] = "7" * 40
        version, url = self.check("o/r@v1.0")
        self.assertEqual(version, "777777777777")
        self.assertEqual(url, "https://codeload.github.com/o/r/zip/refs/tags/v1.0")

    def test_a_branch_still_gets_the_branch_archive_path(self):
        self.assertEqual(self.check("o/r@dev")[1],
                         "https://codeload.github.com/o/r/zip/refs/heads/dev")

    # -- and when it is not there --------------------------------------------

    def test_everything_falls_back_when_git_is_unreachable(self):
        def refuse(request, timeout=0):
            if "info/refs" in request.full_url:
                raise urllib.error.URLError("blocked")
            self.api.append(request.full_url.split("/repos/")[1])
            return FakeResponse(b'{"default_branch": "main"}',
                                {"etag": 'W/"x"', "x-ratelimit-remaining": "50"})
        addons.urllib.request.urlopen = refuse
        addons.begin_run()
        self.assertEqual(addons.default_branch("o/r"), "main")
        # The point is not that it survived -- it is that it went and asked the
        # API, which is the path this whole shortcut exists to skip.
        self.assertEqual(self.api, ["o/r"])

    def test_an_unreadable_advertisement_is_not_trusted(self):
        def rubbish(request, timeout=0):
            if "info/refs" in request.full_url:
                return FakeResponse(b"<html>not git</html>")
            self.api.append(request.full_url.split("/repos/")[1])
            return FakeResponse(b'{"default_branch": "main"}',
                                {"etag": 'W/"x"', "x-ratelimit-remaining": "50"})
        addons.urllib.request.urlopen = rubbish
        addons.begin_run()
        self.assertIsNone(addons.git_refs("o/r"))
        self.assertEqual(addons.default_branch("o/r"), "main")
        self.assertEqual(self.api, ["o/r"])

    def test_a_ref_we_cannot_see_is_never_called_unchanged(self):
        # "I do not know" must not be reported as "nothing moved": the cost of
        # that mistake is an addon that quietly stops updating.
        addons.begin_run()
        self.assertTrue(addons.ref_moved("o/r", "no-such-branch"))


class CheckingWithoutTheAPI(unittest.TestCase):
    """The opt-in mode that spends no REST quota at all, ever.

    Two questions still reach the API in the ordinary path -- which commit last
    touched a folder, and what a release has attached to it. The first has a
    free answer: the archive comes from codeload, which is not the API, so the
    folder can be hashed instead of asked about. The second does not, and this
    mode gives it up: a release asset is a file the author uploaded, it is not
    in the repository, and no amount of downloading the repository will find
    it. Addons checked this way follow their default branch instead.
    """

    REPO = {
        "repo-main/AscensionHonorTracker/AscensionHonorTracker.toc": "a",
        "repo-main/AscensionHonorTracker/main.lua": "print(1)",
        "repo-main/GnomeWorks/GnomeWorks.toc": "b",
        "repo-main/README.md": "docs",
    }

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self._config = addons.CONFIG_DIR
        addons.CONFIG_DIR = pathlib.Path(self.scratch.name)
        self.real_throttle = addons.THROTTLE
        addons.THROTTLE = addons.Throttle(sleep=lambda _s: None, clock=lambda: 0.0)
        self.stub = addons.git_refs
        addons.git_refs = _real_git_refs
        addons.forget_github_state()
        self.addCleanup(addons.forget_github_state)
        self.addCleanup(lambda: setattr(addons, "git_refs", self.stub))
        self.addCleanup(lambda: setattr(addons, "THROTTLE", self.real_throttle))
        self.addCleanup(lambda: setattr(addons, "CONFIG_DIR", self._config))

        self.files = dict(self.REPO)
        self.head = "1" * 40
        self.api, self.downloads = [], []

        def fake(request, timeout=0):
            url = request.full_url
            if "info/refs" in url:
                return FakeResponse(advertisement("main", {"refs/heads/main": self.head}))
            if "codeload" in url:
                self.downloads.append(url)
                return FakeResponse(mkzip(self.files))
            self.api.append(url)
            return FakeResponse(b"{}", {"x-ratelimit-remaining": "50"})

        real = addons.urllib.request.urlopen
        addons.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(addons.urllib.request, "urlopen", real))

    # -- what counts as an addon in an archive -------------------------------

    def test_only_folders_holding_their_own_toc_are_addons(self):
        digests = addons.digests_in_archive(mkzip(self.files))
        self.assertEqual(sorted(digests), ["AscensionHonorTracker", "GnomeWorks"])

    def test_a_folder_one_level_down_still_counts(self):
        # src/MyAddon/MyAddon.toc beside a docs/ folder is a real layout.
        digests = addons.digests_in_archive(mkzip({
            "repo-main/src/MyAddon/MyAddon.toc": "x",
            "repo-main/docs/guide.md": "y",
        }))
        self.assertEqual(sorted(digests), ["src/MyAddon"])

    def test_a_bundled_library_is_not_offered_as_an_addon(self):
        # MyAddon/Libs/AceGUI-3.0 holds AceGUI-3.0.toc and is an addon by the
        # letter of the rule. Nobody choosing what to install means it.
        digests = addons.digests_in_archive(mkzip({
            "repo-main/MyAddon/MyAddon.toc": "x",
            "repo-main/MyAddon/Libs/AceGUI-3.0/AceGUI-3.0.toc": "lib",
        }))
        self.assertEqual(sorted(digests), ["MyAddon"])

    def test_a_bundled_library_counts_toward_its_addon(self):
        # It is not offered as an addon of its own, but a change inside it IS a
        # change to the addon that ships it. An addon that did not notice its
        # own bundled code moving would be worse than having no digest at all.
        before = addons.digests_in_archive(mkzip({
            "repo-main/MyAddon/MyAddon.toc": "x",
            "repo-main/MyAddon/Libs/AceGUI-3.0/AceGUI-3.0.toc": "lib",
        }))
        after = addons.digests_in_archive(mkzip({
            "repo-main/MyAddon/MyAddon.toc": "x",
            "repo-main/MyAddon/Libs/AceGUI-3.0/AceGUI-3.0.toc": "lib updated",
        }))
        self.assertNotEqual(before["MyAddon"], after["MyAddon"])

    def test_a_toc_named_differently_from_its_folder_is_not_an_addon(self):
        self.assertEqual(addons.digests_in_archive(mkzip({
            "repo-main/Something/Other.toc": "x",
        })), {})

    # -- the digest is a version ---------------------------------------------

    def test_the_same_contents_give_the_same_version(self):
        first = addons.digests_in_archive(mkzip(self.files))
        second = addons.digests_in_archive(mkzip(dict(self.files)))
        self.assertEqual(first, second)

    def test_changing_a_file_moves_that_addon_and_only_that_addon(self):
        # The whole point over "the last commit that touched it": an addon
        # reports an update when its OWN files move, and not when its
        # neighbour's do.
        before = addons.digests_in_archive(mkzip(self.files))
        moved = dict(self.files)
        moved["repo-main/GnomeWorks/GnomeWorks.toc"] = "changed"
        after = addons.digests_in_archive(mkzip(moved))
        self.assertNotEqual(before["GnomeWorks"], after["GnomeWorks"])
        self.assertEqual(before["AscensionHonorTracker"], after["AscensionHonorTracker"])

    # -- what it costs -------------------------------------------------------

    def test_a_whole_repo_binding_downloads_nothing_to_check(self):
        # The ref advertisement already carries the commit, so there is nothing
        # to fetch and nothing to hash.
        addons.begin_run()
        version, _url = addons.offline_version("o/r")
        addons.end_run()
        self.assertEqual(version, "1" * 12)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.api, [])

    def test_a_folder_binding_downloads_once_and_asks_the_api_nothing(self):
        addons.begin_run()
        addons.offline_version("o/r#GnomeWorks")
        addons.end_run()
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.api, [])

    def test_every_folder_in_one_repo_shares_that_download(self):
        # Nine addons in one repository must not be nine downloads of the same
        # archive; the digests for all of them come out of the one pass.
        addons.begin_run()
        for folder in ("AscensionHonorTracker", "GnomeWorks"):
            addons.offline_version(f"o/r#{folder}")
        addons.end_run()
        self.assertEqual(len(self.downloads), 1)

    def test_a_later_run_downloads_nothing_while_the_commit_stands(self):
        # A digest is kept against the commit it was taken from, so it is never
        # stale and never computed twice.
        addons.begin_run(); addons.offline_version("o/r#GnomeWorks"); addons.end_run()
        self.downloads.clear()
        addons.begin_run(); addons.offline_version("o/r#GnomeWorks"); addons.end_run()
        self.assertEqual(self.downloads, [])

    def test_a_new_commit_is_fetched_again(self):
        addons.begin_run(); first = addons.offline_version("o/r#GnomeWorks")[0]; addons.end_run()
        self.head = "2" * 40
        self.files["repo-main/GnomeWorks/GnomeWorks.toc"] = "changed"
        self.downloads.clear()
        addons.begin_run(); second = addons.offline_version("o/r#GnomeWorks")[0]; addons.end_run()
        self.assertEqual(len(self.downloads), 1)
        self.assertNotEqual(first, second)

    def test_a_named_folder_that_is_not_there_is_reported(self):
        addons.begin_run()
        with self.assertRaises(addons.Fail) as caught:
            addons.offline_version("o/r#Nonexistent")
        self.assertIn("Nonexistent", str(caught.exception))

    def test_the_dialog_can_list_folders_without_the_api(self):
        addons.begin_run()
        found = addons.addons_in_repo("o/r", offline=True)
        addons.end_run()
        self.assertEqual(found, ["AscensionHonorTracker", "GnomeWorks"])
        self.assertEqual(self.api, [])

    # -- and through the front door ------------------------------------------

    def test_update_addon_offline_installs_and_spends_no_quota(self):
        root = pathlib.Path(self.scratch.name) / "AddOns"
        root.mkdir()
        entry = {"source": "github:o/r#GnomeWorks", "mode": "link"}
        addons.begin_run()
        result = addons.update_addon("GnomeWorks", entry, root, offline=True)
        addons.end_run()
        self.assertEqual(result.outcome, addons.CHANGED, result.detail)
        self.assertTrue((root / "GnomeWorks" / "GnomeWorks.toc").is_file())
        self.assertEqual(self.api, [])

    def test_the_check_and_the_install_share_one_download(self):
        # The check has just fetched this exact archive to work the version
        # out; the install should not pay for it a second time.
        root = pathlib.Path(self.scratch.name) / "AddOns2"
        root.mkdir()
        addons.begin_run()
        addons.update_addon("GnomeWorks", {"source": "github:o/r#GnomeWorks"}, root, offline=True)
        addons.end_run()
        self.assertEqual(len(self.downloads), 1)

    def test_the_setting_is_remembered_per_install(self):
        install = addons.blank_install()
        self.assertFalse(addons.checks_offline(install))
        addons.set_checks_offline(install, True)
        self.assertTrue(addons.checks_offline(install))


class RescanForgetsWhatYouDeleted(unittest.TestCase):
    """A rescan must clear out addons you deleted -- but not your bindings.

    Reported after v0.8.0: deleting an addon folder by hand and rescanning left
    the row in the list for ever, because nothing else in this tool removes
    one. The fix cannot be "drop every row whose folder is gone": a bound row
    with no folder is how an addon you have bound but not yet fetched appears,
    and the binding is the one thing in the manifest that scanning cannot
    recreate.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "AddOns"
        self.root.mkdir()
        for name in ("HandInstalled", "BoundOne", "Stays"):
            (self.root / name).mkdir()
            (self.root / name / f"{name}.toc").write_text(f"## Title: {name}\n")
        self.state = addons.migrate(addons.blank_install())
        self.install = addons.current(self.state)
        self.install["addons_dir"] = str(self.root)
        addons.rescan(self.install, self.root)
        addons.set_source(self.install, "BoundOne", "github:someone/BoundOne")

    def entries(self):
        return self.install["addons"]

    def test_an_unmanaged_addon_you_deleted_is_dropped(self):
        shutil.rmtree(self.root / "HandInstalled")
        _installed, _guessed, forgotten = addons.rescan(self.install, self.root)
        self.assertEqual(forgotten, 1)
        self.assertNotIn("HandInstalled", self.entries())

    def test_a_bound_addon_you_deleted_keeps_its_binding(self):
        shutil.rmtree(self.root / "BoundOne")
        _installed, _guessed, forgotten = addons.rescan(self.install, self.root)
        self.assertEqual(forgotten, 0)
        self.assertIn("BoundOne", self.entries())
        self.assertTrue(self.entries()["BoundOne"]["missing"])
        self.assertEqual(self.entries()["BoundOne"]["source"], "github:someone/BoundOne")

    def test_what_is_still_there_is_left_alone(self):
        shutil.rmtree(self.root / "HandInstalled")
        addons.rescan(self.install, self.root)
        self.assertIn("Stays", self.entries())
        self.assertNotIn("missing", self.entries()["Stays"])

    def test_unmanaging_a_row_is_how_you_get_rid_of_it(self):
        # The escape hatch, and the reason keeping bound rows is not a trap:
        # set the source to unmanaged, rescan, and the row goes.
        shutil.rmtree(self.root / "BoundOne")
        addons.rescan(self.install, self.root)
        addons.set_source(self.install, "BoundOne", "unmanaged")
        _installed, _guessed, forgotten = addons.rescan(self.install, self.root)
        self.assertEqual(forgotten, 1)
        self.assertNotIn("BoundOne", self.entries())

    def test_a_deleted_addon_does_not_come_back_as_a_suggestion(self):
        shutil.rmtree(self.root / "HandInstalled")
        addons.rescan(self.install, self.root)
        addons.rescan(self.install, self.root)
        self.assertNotIn("HandInstalled", self.entries())
