"""Command-line checks for selective white-box builds."""

import csv
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT = SCRIPT_DIR / "dist-wb-selected.py"


class SelectedWhiteBoxBuildTest(unittest.TestCase):
    """Check app selection without starting Maven or Gradle builds."""

    def run_script(self, *arguments):
        """Run the script with the requested command-line arguments."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=SCRIPT_DIR.parent,
            capture_output=True,
            text=True,
        )

    def test_lists_names_from_manifest(self):
        """List every supported white-box app from the manifest."""
        with (SCRIPT_DIR / "dist-wb-apps.csv").open(newline="", encoding="utf-8") as manifest:
            expected = {row["name"] for row in csv.DictReader(manifest)}

        result = self.run_script("--list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), expected)

    def test_dry_run_builds_only_requested_apps(self):
        """Select apps from different JDKs without building their neighbors."""
        result = self.run_script("--dry-run", "blogapi", "arimaa", "blogapi")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Build blogapi:"), 1)
        self.assertIn("Build arimaa:", result.stdout)
        self.assertIn("blogapi-evomaster-runner.jar", result.stdout)
        self.assertIn("arimaa-sut.jar", result.stdout)
        self.assertNotIn("redis-sample", result.stdout)

    def test_unknown_app_is_rejected(self):
        """Reject a misspelled app before running any build."""
        result = self.run_script("--dry-run", "unknown-app")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown app(s): unknown-app", result.stderr)
        self.assertNotIn("Build ", result.stdout)


if __name__ == "__main__":
    unittest.main()
