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

import os
import pathlib
import re
import struct
import unittest
import xml.dom.minidom

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECIPE = ROOT / "packaging" / "appimage" / "WoW-Addons-from-GitHub"


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
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_icon", ROOT / "packaging" / "appimage" / "make_icon.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "icon.png"
            module.write_png(out, module.render())
            self.assertEqual(
                out.read_bytes(),
                (RECIPE / "wow-addons-from-github.png").read_bytes(),
                "re-run packaging/appimage/make_icon.py and commit the result",
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
