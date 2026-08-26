#!/usr/bin/env python3
"""Checks on the AppImage recipe that do not need a network or a build.

The build itself needs to download a base image, so it only really happens in
CI. These run everywhere and catch the mistakes that would otherwise surface as
a failed release build twenty minutes after tagging: a renamed icon, an
entry point that stopped going through the Tcl/Tk wrapper, a desktop file whose
Icon= no longer matches any file.

The one to care about is `test_the_entry_point_goes_through_the_tk_wrapper`.
python-appimage's `{{ python-executable }}` is the usr/bin wrapper script that
exports TCL_LIBRARY, TK_LIBRARY and TKPATH; the real interpreter under opt/ does
not. Calling the latter produces an AppImage that starts, prints help, passes a
CLI smoke test -- and then cannot open a window.
"""

import ast
import os
import pathlib
import re
import struct
import sys
import unittest
import xml.dom.minidom

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECIPE = ROOT / "packaging" / "appimage" / "WoW-Addons-from-GitHub"


def pyinstaller_flags():
    """The strings actually passed to PyInstaller, read out of build.py's AST.

    Reading the source as text instead would match the comments that explain
    these choices as readily as the choices themselves -- which is how the
    first version of these tests failed, asserting `--onefile` was absent from
    a file whose docstring says "--onedir, not --onefile".
    """
    tree = ast.parse((ROOT / "packaging" / "windows" / "build.py").read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run"):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        strings = [e.value for e in node.args[0].elts if isinstance(e, ast.Constant)
                   and isinstance(e.value, str)]
        if "PyInstaller" in strings:
            return strings
    raise AssertionError("build.py has no run([...]) call invoking PyInstaller")


def code_without_prose(path: pathlib.Path) -> str:
    """Source with docstrings removed, so a test cannot match an explanation."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def load_icon_generator():
    """Import packaging/make_icon.py by path; it is a script, not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "packaging" / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def substitute(text: str, **values) -> str:
    """python-appimage's {{ key }} templating, reimplemented for the test.

    Deliberately a copy rather than an import: these tests must run without
    python-appimage installed, which is the normal state of a checkout.
    """
    for key, value in values.items():
        text = text.replace("{{ %s }}" % key, value)
    return text


class Recipe(unittest.TestCase):
    def test_the_recipe_has_everything_the_builder_looks_for(self):
        for name in (
            "requirements.txt",
            "entrypoint.sh",
            "wow-addons-from-github.desktop",
            "wow-addons-from-github.png",
            "wow-addons-from-github.appdata.xml",
        ):
            self.assertTrue((RECIPE / name).is_file(), f"{name} is missing from the recipe")

    def test_the_entry_point_goes_through_the_tk_wrapper(self):
        entry = substitute(
            (RECIPE / "entrypoint.sh").read_text(),
            **{"python-executable": "${APPDIR}/usr/bin/python3.12", "python-version": "3.12"},
        )
        self.assertIn("${APPDIR}/usr/bin/python3.12", entry)
        self.assertNotIn("/opt/python", entry, "that path skips the Tcl/Tk environment")
        self.assertIn("-m wowaddons", entry)
        self.assertIn('"$@"', entry, "arguments must reach the CLI")
        self.assertTrue(entry.startswith("#!"), "python-appimage takes the shebang from line one")

    def test_no_template_placeholder_is_left_unfilled(self):
        # A typo'd key stays in the file verbatim and becomes a shell syntax
        # error at launch, which is a miserable thing to debug from a bug report.
        known = {"python-executable", "python-version", "python-fullversion",
                 "python-tag", "linux-tag", "architecture", "requirements"}
        for path in RECIPE.iterdir():
            if path.suffix in (".png",):
                continue
            for placeholder in re.findall(r"\{\{\s*([\w.-]+)\s*\}\}", path.read_text()):
                self.assertIn(placeholder, known, f"{path.name} uses an unknown placeholder")

    def desktop_field(self, key: str) -> str:
        for line in (RECIPE / "wow-addons-from-github.desktop").read_text().splitlines():
            if line.startswith(key + "="):
                return line[len(key) + 1:].strip()
        self.fail(f"the desktop file has no {key}=")

    def test_the_desktop_file_names_the_app_and_an_icon_that_exists(self):
        # Name= is both the menu entry and the built filename; Icon= has to
        # match a real file or the AppImage ships with the generic Python icon.
        self.assertEqual(self.desktop_field("Name"), "WoW Addons from GitHub")
        icon = self.desktop_field("Icon")
        images = [p for p in RECIPE.glob(icon + ".*") if p.suffix in (".png", ".svg")]
        self.assertEqual(len(images), 1, f"expected exactly one image named {icon}.*, got {images}")

    def test_the_desktop_file_does_not_ask_for_a_terminal(self):
        self.assertEqual(self.desktop_field("Terminal"), "false")

    def test_the_icon_is_a_png_whose_header_gives_its_size(self):
        # build/app.py reads width and height straight out of the IHDR to decide
        # which hicolor directory to install into, so a malformed header is a
        # build failure rather than a cosmetic problem.
        head = (RECIPE / "wow-addons-from-github.png").read_bytes()[:24]
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">ii", head[16:24]), (256, 256))

    def test_the_icon_matches_what_the_generator_draws(self):
        # Regenerating must be a no-op, or the committed icon and the script
        # that claims to draw it have drifted.
        module = load_icon_generator()

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "icon.png"
            module.write_png(out, module.render())
            self.assertEqual(
                out.read_bytes(),
                (RECIPE / "wow-addons-from-github.png").read_bytes(),
                "re-run packaging/make_icon.py and commit the result",
            )

    def test_the_only_requirement_is_the_local_package(self):
        # The tool has no dependencies and the AppImage must not acquire any.
        lines = [
            line.strip()
            for line in (RECIPE / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(lines, ["local+wowaddons"])

    def test_the_appdata_is_well_formed_and_points_at_the_desktop_file(self):
        document = xml.dom.minidom.parse(str(RECIPE / "wow-addons-from-github.appdata.xml"))
        launchable = document.getElementsByTagName("launchable")[0]
        self.assertEqual(launchable.firstChild.nodeValue, "wow-addons-from-github.desktop")


class WindowsIcon(unittest.TestCase):
    """The .ico PyInstaller embeds. Malformed, the build fails or ships blank.

    Written by hand from zlib and struct, so the format details are ours to get
    wrong: an entry whose recorded size disagrees with its payload, or a 256
    that forgot to record itself as 0, both produce a file Windows quietly
    refuses to draw.
    """

    ICO = ROOT / "packaging" / "windows" / "wow-addons-from-github.ico"

    def entries(self):
        data = self.ICO.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1), "not an icon directory")
        found = []
        for index in range(count):
            head = data[6 + 16 * index: 6 + 16 * (index + 1)]
            width, height, palette, _r, planes, bpp, size, offset = struct.unpack("<BBBBHHII", head)
            found.append({
                "width": width or 256, "height": height or 256, "palette": palette,
                "planes": planes, "bpp": bpp, "blob": data[offset:offset + size],
                "declared": size,
            })
        return found

    def test_it_carries_the_sizes_windows_asks_for(self):
        # 16 is the taskbar and title bar; a 256 scaled down to it turns to mush,
        # which is the entire reason each size is drawn at its own scale.
        self.assertEqual(sorted(e["width"] for e in self.entries()), [16, 32, 48, 256])

    def test_every_entry_is_a_png_of_the_size_it_claims(self):
        for entry in self.entries():
            self.assertEqual(entry["blob"][:8], b"\x89PNG\r\n\x1a\n", "entry is not a PNG")
            self.assertEqual(len(entry["blob"]), entry["declared"], "declared size is wrong")
            width, height = struct.unpack(">ii", entry["blob"][16:24])
            self.assertEqual((width, height), (entry["width"], entry["height"]),
                             "the directory and the PNG header disagree")

    def test_entries_describe_full_colour_images(self):
        for entry in self.entries():
            self.assertEqual(entry["bpp"], 32)
            self.assertEqual(entry["planes"], 1)
            self.assertEqual(entry["palette"], 0, "0 means not paletted")

    def test_the_icons_match_what_the_generator_draws(self):
        module = load_icon_generator()
        self.assertEqual(
            module.ico_bytes({size: module.png_bytes(module.render(size)) for size in module.ICO_SIZES}),
            self.ICO.read_bytes(),
            "re-run packaging/make_icon.py and commit the result",
        )

    def test_both_platforms_are_drawn_from_one_script(self):
        # If these ever came from separate sources the two builds would drift
        # into looking like different programs.
        source = (ROOT / "packaging" / "make_icon.py").read_text()
        self.assertIn("PNG_PATH", source)
        self.assertIn("ICO_PATH", source)


class WindowsBuild(unittest.TestCase):
    BUILD = ROOT / "packaging" / "windows" / "build.py"

    def test_it_builds_a_folder_rather_than_a_single_file(self):
        # A one-file build is a self-extracting archive, which is what a lot of
        # malware looks like, so heuristic antivirus flags it far more often.
        flags = pyinstaller_flags()
        self.assertIn("--onedir", flags)
        self.assertNotIn("--onefile", flags)

    def test_it_is_windowed_so_double_clicking_opens_a_window(self):
        self.assertIn("--windowed", pyinstaller_flags())

    def test_it_names_the_lazily_imported_gui_module(self):
        # wowaddons.gui is imported inside a function, inside a try/except, so a
        # missing Tk degrades to a message. That is invisible to a static
        # analyser: without the hidden import the build succeeds and the .exe
        # has no window at all.
        flags = pyinstaller_flags()
        self.assertIn("wowaddons.gui", flags)
        self.assertIn("tkinter", flags)
        self.assertEqual(flags.count("--hidden-import"), 2)

    def test_the_entry_point_does_not_reuse_the_checkout_launcher(self):
        # addons.py exists to put the repo root on sys.path, which is precisely
        # wrong inside a frozen build, where the package is already bundled.
        entry = code_without_prose(ROOT / "packaging" / "windows" / "entry.py")
        self.assertIn("from wowaddons.__main__ import main", entry)
        self.assertNotIn("sys.path", entry)


class NothingWindowsCannotCheckOut(unittest.TestCase):
    """No tracked path may be a name Windows reserves for a device.

    This is not a style rule. `git checkout` on Windows refuses a repository
    containing one, with "invalid path 'CONOUT$'" and nothing else -- not the
    file, the whole clone. Every Windows contributor and every Windows CI job
    fails at checkout, before a single test runs.

    It happened here: a test called winconsole._reopen() off Windows, where
    CONOUT$ is an ordinary filename rather than a console device, and the empty
    file it left behind was committed.
    """

    # Device names MS-DOS reserved and Windows still honours, plus the console
    # handles. Reserved with any extension, and case-insensitively.
    RESERVED = {
        "con", "prn", "aux", "nul", "conout$", "conin$",
        *(f"com{n}" for n in range(1, 10)),
        *(f"lpt{n}" for n in range(1, 10)),
    }

    def tracked_paths(self):
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        return [p for p in result.stdout.split("\0") if p]

    def test_no_tracked_path_uses_a_reserved_device_name(self):
        paths = self.tracked_paths()
        self.assertTrue(paths, "git ls-files returned nothing")
        offenders = [
            path
            for path in paths
            for part in pathlib.PurePosixPath(path).parts
            if part.split(".")[0].lower() in self.RESERVED
        ]
        self.assertEqual(offenders, [], "these break `git checkout` on Windows")

    def test_no_tracked_path_uses_a_character_windows_forbids(self):
        # Same failure mode, different cause: a colon or a trailing dot in a
        # tracked name makes the clone impossible to check out on Windows.
        offenders = [
            path for path in self.tracked_paths()
            if set(path) & set('<>:"|?*') or any(
                part != part.rstrip(" .") for part in pathlib.PurePosixPath(path).parts
            )
        ]
        self.assertEqual(offenders, [], "these break `git checkout` on Windows")


class Releasing(unittest.TestCase):
    """The release machinery, which is only ever exercised for real on a tag.

    Everything here is cheap and checked on every push, because the alternative
    is finding out at the moment somebody is trying to cut a release.
    """

    NOTES = ROOT / ".github" / "release-notes.md"
    WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

    def workflow_commands(self) -> str:
        """The workflow with its comments stripped.

        Comments here quote the very command shapes these tests forbid, so
        matching the raw file finds the explanation as readily as the mistake --
        which is exactly how the first version of the test below failed.
        """
        return "\n".join(
            line for line in self.WORKFLOW.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )

    def workflow_jobs(self) -> dict:
        """Each job in the workflow, as name -> its own block of the file.

        No PyYAML in a stdlib-only project, and none is needed: jobs are the
        keys indented four spaces under `jobs:`, and a job owns every line
        until the next one.
        """
        jobs, name = {}, None
        inside = False
        for line in self.workflow_commands().splitlines():
            if line.rstrip() == "jobs:":
                inside = True
                continue
            if not inside:
                continue
            if re.fullmatch(r"  ([\w-]+):", line.rstrip()):
                name = line.strip().rstrip(":")
                jobs[name] = []
            elif name:
                jobs[name].append(line)
        return {name: "\n".join(lines) for name, lines in jobs.items()}

    def test_every_job_that_reads_a_repo_file_checks_the_repo_out(self):
        """A job with no checkout has an empty workspace, artifacts aside.

        `publish` had no checkout and got away with it while the notes file was
        only read on a branch that never ran. The moment `gh release edit`
        started reading it on every tag, v0.3.1 died on `no such file or
        directory` -- after both builds had spent their five minutes, and with
        the release already public and untitled.
        """
        for name, body in self.workflow_jobs().items():
            wanted = sorted({
                path for path in re.findall(r"[\w.][\w./-]*\.(?:md|sh|py|txt|cfg|toml)", body)
                if (ROOT / path).is_file()
            })
            if not wanted:
                continue
            self.assertIn(
                "actions/checkout", body,
                f"job {name!r} reads {wanted} but never checks the repository out",
            )

    def test_the_checkout_comes_before_the_download(self):
        # checkout cleans the workspace before it fetches, so a checkout after
        # actions/download-artifact deletes the artifacts it just downloaded.
        publish = self.workflow_jobs()["publish"]
        self.assertIn("actions/checkout", publish)
        self.assertLess(
            publish.index("actions/checkout"), publish.index("download-artifact"),
            "checkout would wipe the downloaded artifacts",
        )

    def test_the_notes_have_a_section_for_the_version_being_shipped(self):
        """Bumping the version and writing the notes are one job, not two.

        The regression: v0.7.0 was dispatched against code still calling itself
        0.6.0. `version-matches-tag` caught it, which is what it is for -- but
        it caught it after somebody had already typed a tag and started a
        release, and the only signal before that was remembering. This fails on
        every push instead, the moment the version moves without its notes.

        It also holds the other way round: notes for a version that is not the
        one shipping are notes nobody will read.
        """
        import wowaddons

        heading = re.compile(
            rf"^## (?:New|Fixed)[\w ]* in v{re.escape(wowaddons.__version__)}$",
            re.MULTILINE,
        )
        # searched rather than assertRegex'd: a failure here should say what is
        # missing, not print the whole notes file back at you.
        self.assertTrue(
            heading.search(self.NOTES.read_text()),
            f"release-notes.md has no '## New in v{wowaddons.__version__}' section -- "
            "bump the version and describe it in the same commit",
        )

    def test_the_version_is_reportable(self):
        # A shipped binary that cannot say which build it is makes every bug
        # report start with a guessing game.
        import subprocess

        result = subprocess.run(
            [sys.executable, str(ROOT / "addons.py"), "--version"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import wowaddons

        self.assertIn(wowaddons.__version__, result.stdout)

    def test_the_window_shows_the_version_too(self):
        # A GUI has no --version, so the title bar is where it has to appear.
        gui = (ROOT / "wowaddons" / "gui.py").read_text()
        self.assertIn("__version__", gui)

    def test_the_release_notes_exist_where_the_workflow_looks(self):
        # gh release create --notes-file fails the publish if this is missing,
        # after both builds have already run.
        workflow = self.WORKFLOW.read_text()
        self.assertIn("--notes-file notes.md", workflow)
        self.assertTrue(self.NOTES.is_file())
        self.assertGreater(len(self.NOTES.read_text()), 500, "notes look like a stub")

    def test_the_published_notes_are_built_from_the_ones_in_the_repo(self):
        """`--notes-file notes.md` is only safe if something writes notes.md.

        The download table at the top of the notes links straight at the
        assets, which needs the tag -- and a file in the repository cannot know
        the tag. The publish step substitutes it into a copy. If that step is
        dropped or renamed, `--notes-file` starts pointing at a file nobody
        creates and the release publishes with no notes at all.
        """
        commands = self.workflow_commands()
        self.assertIn("release-notes.md > notes.md", commands)
        self.assertLess(
            commands.index("release-notes.md > notes.md"),
            commands.index("--notes-file notes.md"),
            "notes.md is used before anything writes it",
        )

    def test_the_download_links_are_per_release(self):
        """Each release must link to its own assets, not to whatever is newest.

        `/releases/latest/download/...` would need no substitution and is wrong
        here: every release this workflow publishes is marked a pre-release, and
        `latest` skips pre-releases, so those links would 404 for as long as
        that stays true.
        """
        notes = self.NOTES.read_text()
        self.assertIn("/releases/download/__TAG__/", notes)
        self.assertNotIn("/releases/latest/download/", notes)

    def test_the_notes_lead_with_the_download(self):
        # GitHub renders its own Assets block at the foot of the page and
        # nothing can move it, so the notes carry the links themselves -- above
        # the changelog, or they are four versions of history down the page.
        headings = [line for line in self.NOTES.read_text().splitlines()
                    if line.startswith("## ")]
        self.assertEqual(headings[0], "## Download")

    def test_the_notes_name_the_files_the_builds_actually_produce(self):
        # Release notes telling somebody to download a filename that does not
        # exist is the first thing they see and the easiest thing to get wrong.
        notes = self.NOTES.read_text()
        self.assertIn("WoW-Addons-from-GitHub-x86_64.AppImage", notes)
        self.assertIn("WoW-Addons-from-GitHub-windows-x64", notes)

        appimage = (ROOT / "packaging/appimage/build.sh").read_text()
        self.assertIn("WoW-Addons-from-GitHub-$ARCH.AppImage", appimage)
        windows = (ROOT / "packaging/windows/build.py").read_text()
        self.assertIn('"WoW-Addons-from-GitHub-windows-x64"', windows)

    def test_the_notes_warn_about_both_first_run_scares(self):
        # SmartScreen and the missing libfuse2 are the two things that look
        # like faults and are not. Leaving either out generates bug reports.
        notes = self.NOTES.read_text().lower()
        self.assertIn("smartscreen", notes)
        self.assertIn("libfuse", notes)

    def test_the_metadata_is_applied_even_when_the_release_exists(self):
        """`view || create` silently drops the notes and the pre-release flag.

        It reads as harmless idempotency. It is not: creating the tag through
        "Draft a new release" in the web UI -- which is how most people make a
        tag -- publishes the release before the workflow runs, so `view`
        succeeds, `create` is skipped, and the title, notes and --prerelease go
        with it. v0.3.0 published exactly that way: right assets, no title, no
        notes, not a pre-release, and the step exited 0.
        """
        commands = self.workflow_commands()
        self.assertIn("gh release edit", commands, "nothing applies metadata to an existing release")
        self.assertNotIn("|| gh release create", commands, "the bug that shipped v0.3.0 is back")
        # Both branches must set all three, or one route silently differs.
        self.assertEqual(commands.count("--notes-file notes.md"), 2)
        self.assertEqual(commands.count("--prerelease"), 2)

    def test_the_publish_verifies_what_it_published(self):
        # The failing step exited 0. Checking the exit codes was not enough,
        # so the step now reads the release back and fails on wrong metadata.
        commands = self.workflow_commands()
        self.assertIn("the release published without its metadata", commands)
        self.assertIn("isPrerelease", commands)

    def test_a_release_can_be_rebuilt_without_touching_git(self):
        """The only repair route for a failed release that a browser can drive.

        The workflow file that runs is the one at the ref you dispatch from, so
        dispatching main with a tag named rebuilds that tag against a workflow
        fixed since. Without the input the only repair is deleting and
        recreating the tag, and a release whose build half-failed cannot be
        fixed by anyone without a git checkout.
        """
        workflow = self.WORKFLOW.read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertRegex(workflow, r"inputs:\s*\n\s+tag:")
        for job in ("version-matches-tag", "publish"):
            self.assertIn(
                "inputs.tag != ''", self.workflow_jobs()[job],
                f"job {job!r} still only runs for a tag push, so a dispatch publishes nothing",
            )

    def test_nothing_names_the_release_after_the_ref_it_ran_from(self):
        # GITHUB_REF_NAME is "main" on a dispatch. Left anywhere in the publish
        # job it would create a release literally called main, and upload the
        # binaries to it.
        self.assertNotIn(
            "GITHUB_REF_NAME", self.WORKFLOW.read_text(),
            "use RELEASE_TAG: a dispatched run would publish a release named after the branch",
        )

    def test_the_builds_check_out_what_is_being_released(self):
        """The tag when it exists, the named commit when it does not.

        Otherwise a dispatched rebuild ships main's code under the tag's name --
        the exact mismatch version-matches-tag exists to prevent.
        """
        jobs = self.workflow_jobs()
        for job in ("appimage", "windows", "version-matches-tag", "publish"):
            self.assertIn(
                "ref: ${{ inputs.build_from || inputs.tag }}", jobs[job],
                f"job {job!r} would build the dispatch ref instead of the release",
            )

    def test_a_brand_new_tag_can_be_cut_from_the_actions_tab(self):
        """Someone who works only in a browser has no way to make a tag.

        The `tag` input alone could only rebuild one that already existed --
        the build jobs check it out, so a tag that does not exist yet fails at
        checkout before anything else runs. `build_from` names the commit to
        build and tag instead.
        """
        workflow = self.WORKFLOW.read_text()
        self.assertRegex(workflow, r"inputs:\s*\n\s+tag:")
        self.assertIn("build_from:", workflow)
        self.assertIn("--target", self.workflow_commands(),
                      "gh release create must be told which commit to tag")

    def test_the_commit_to_tag_is_read_from_the_checkout(self):
        """Not from github.sha, which is a different commit.

        github.sha is the DISPATCH ref's head. Set build_from to anything other
        than the branch the workflow was dispatched from and the two differ --
        so the release would be tagged at one commit while carrying binaries
        built from another, with nothing to show they disagreed.
        """
        commands = self.workflow_commands()
        self.assertIn("RELEASE_COMMIT=$(git rev-parse HEAD)", commands)
        self.assertNotIn("RELEASE_COMMIT: ${{ github.sha }}", commands)

    def test_the_publish_waits_for_the_version_check(self):
        # Otherwise a mismatched tag still publishes; the check would just go
        # red beside it.
        self.assertIn("needs: [appimage, windows, version-matches-tag]", self.workflow_commands())


class Scripts(unittest.TestCase):
    def test_the_build_and_smoke_scripts_are_executable(self):
        for name in ("build.sh", "smoke-test.sh"):
            path = ROOT / "packaging" / "appimage" / name
            self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")

    def test_the_build_avoids_needing_fuse(self):
        # appimagetool is itself an AppImage, so without this the build fails on
        # any runner or container without FUSE -- which is most of them.
        self.assertIn("APPIMAGE_EXTRACT_AND_RUN=1", (ROOT / "packaging/appimage/build.sh").read_text())

    def test_the_build_packages_the_appdir_itself(self):
        # python-appimage joins the appimagetool command with spaces and runs it
        # through a shell without quoting, so an output name containing spaces
        # -- which ours does, because the desktop Name= belongs in a menu --
        # gets split into separate arguments and nothing is written. Building
        # with --no-packaging and invoking appimagetool here avoids trading a
        # readable menu entry for a working build.
        script = (ROOT / "packaging/appimage/build.sh").read_text()
        self.assertIn("--no-packaging", script)
        self.assertIn("ensure_appimagetool", script)

    def test_the_desktop_name_would_break_the_builders_own_packaging(self):
        # A guard on the comment above: if Name= ever loses its spaces, the
        # --no-packaging detour stops being necessary and can be simplified out.
        name = None
        for line in (RECIPE / "wow-addons-from-github.desktop").read_text().splitlines():
            if line.startswith("Name="):
                name = line[5:].strip()
        self.assertIn(" ", name, "if this is no longer true, revisit build.sh")

    def test_the_build_makes_the_package_importable(self):
        # `local+wowaddons` is resolved with importlib against the build
        # interpreter, so the repo root has to be on the path.
        self.assertIn("PYTHONPATH", (ROOT / "packaging/appimage/build.sh").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
