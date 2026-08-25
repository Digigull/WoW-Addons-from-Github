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
import pathlib
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
            self.assertTrue((self.root / "MyAddon").is_symlink())
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
            self.assertTrue((root / "MyAddon").is_symlink())

    def test_a_symlink_is_not_reported_as_displaced(self):
        # Replacing a link destroys nothing, so warning about it would be noise.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "AddOns"
            root.mkdir()
            source = pathlib.Path(tmp) / "src" / "MyAddon"
            source.mkdir(parents=True)
            (root / "MyAddon").symlink_to(source, target_is_directory=True)
            entry = {"source": f"local:{source}", "mode": "link"}
            self.assertIsNone(addons.will_displace(entry, root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
