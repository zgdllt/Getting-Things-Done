import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gtd
import gtd_notifications


class PortableReleaseTests(unittest.TestCase):
    def test_installed_skill_reuses_the_app_registered_by_portable_startup(self):
        with tempfile.TemporaryDirectory(prefix="gtd-portable-skill-") as directory:
            state = gtd.State(Path(directory) / "state")
            app = state.base / "runtime/Getting Things Done.app"
            binary = app / "Contents/MacOS/gtd-notify"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture executable")
            hosted_skill = app / "Contents/Resources/skill"
            source = hosted_skill / "scripts/gtd_notify.swift"
            source.parent.mkdir(parents=True)
            source.write_text("fixture source")
            installed = Path(directory) / "installed-skill"
            (installed / "scripts").mkdir(parents=True)
            (installed / "scripts/gtd_notify.swift").write_text("fixture source")
            try:
                with patch.object(gtd, "SKILL", hosted_skill), patch.object(gtd, "HERE", source.parent):
                    self.assertEqual(gtd_notifications.bundle(state), app)
                with patch.object(gtd, "SKILL", installed), patch.object(gtd, "HERE", installed / "scripts"), patch.object(gtd.subprocess, "run") as run:
                    self.assertEqual(gtd_notifications.bundle(state), app)
                    run.assert_not_called()
            finally:
                state.db.close()

    def test_packaged_code_reuses_relocated_host_app(self):
        with tempfile.TemporaryDirectory(prefix="relocated app ") as directory:
            app = Path(directory) / "Getting Things Done.app"
            skill = app / "Contents/Resources/skill"
            binary = app / "Contents/MacOS/gtd-notify"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture native executable")
            state = gtd.State(Path(directory) / "private-state")
            try:
                with patch.object(gtd, "SKILL", skill), patch.object(gtd.subprocess, "run") as run:
                    self.assertEqual(gtd_notifications.bundle(state), app)
                    run.assert_not_called()
            finally:
                state.db.close()

    def test_packaged_bridge_is_copied_without_a_compiler_or_source_mutation(self):
        with tempfile.TemporaryDirectory(prefix="relocated app ") as directory:
            root = Path(directory)
            resources = root / "Getting Things Done.app/Contents/Resources"
            skill = resources / "skill"
            source = skill / "scripts/gtd_native.swift"
            source.parent.mkdir(parents=True)
            source.write_text("fixture Swift source")
            bridge = resources / "Personal GTD Bridge.app"
            binary = bridge / "Contents/MacOS/gtd-native"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture prebuilt executable")
            state = gtd.State(root / "private-state")
            try:
                before = {str(p.relative_to(resources)): p.read_bytes() for p in resources.rglob("*") if p.is_file()}
                with patch.object(gtd, "SKILL", skill), patch.object(gtd, "HERE", source.parent), patch.object(gtd.subprocess, "run") as run:
                    installed = gtd.native_binary(state)
                    self.assertEqual(installed.read_text(), "fixture prebuilt executable")
                    self.assertTrue(installed.is_relative_to(state.base))
                    self.assertEqual(gtd.native_binary(state), installed)
                    run.assert_not_called()
                after = {str(p.relative_to(resources)): p.read_bytes() for p in resources.rglob("*") if p.is_file()}
                self.assertEqual(before, after)
            finally:
                state.db.close()


if __name__ == "__main__":
    unittest.main()
