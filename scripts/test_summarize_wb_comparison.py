"""Integration checks for white-box comparison metric extraction."""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("summarize-wb-comparison.py")
MODES = ("no-ddb-heuristics", "ddb-heuristics-no-insertions",
         "ddb-heuristics-with-insertions")


class SummarizeWbComparisonTest(unittest.TestCase):
    """Verify summary rows from real files on disk."""

    def test_all_cycle_modes_become_rows_with_coverage_and_ddb_metrics(self):
        """World Cup comparison cycles retain separate metrics for each mode."""
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary) / "results"
            for cycle in (1, 2):
                for mode in MODES:
                    folder = results / "world-cup" / f"cycle-{cycle:03d}" / mode
                    folder.mkdir(parents=True)
                    (folder / "run.json").write_text(json.dumps({
                        "seed": 26, "evomaster_exit_code": 0,
                        "warnings": ["check coverage"] if cycle == 2 and mode == MODES[2] else [],
                    }), encoding="utf-8")
                    if cycle == 2 and mode == MODES[2]:
                        continue  # A failed run still needs a summary row.
                    fields = ("seed", "generatedTests", "evaluatedTests", "coveredTargets",
                              "coveredLines", "numberOfLines", "coveredBranches",
                              "numberOfBranches", "dynamoDbHeuristicsEvaluationCount",
                              "dynamoDbInsertedRecords")
                    with (folder / "statistics.csv").open("w", newline="", encoding="utf-8") as target:
                        writer = csv.DictWriter(target, fieldnames=fields)
                        writer.writeheader()
                        if cycle == 1 and mode == MODES[0]:
                            writer.writerow({"generatedTests": 1, "coveredLines": 1})
                        writer.writerow({
                            "seed": 26, "generatedTests": 11, "evaluatedTests": 55,
                            "coveredTargets": 90, "coveredLines": 25, "numberOfLines": 40,
                            "coveredBranches": 8, "numberOfBranches": 20,
                            "dynamoDbHeuristicsEvaluationCount": 7 if mode != MODES[0] else 0,
                            "dynamoDbInsertedRecords": 3 if mode == MODES[2] else 0,
                        })
            legacy = results / "world-cup" / "run-001"
            legacy.mkdir()
            (legacy / "statistics.csv").write_text("generatedTests\n999\n", encoding="utf-8")

            completed = subprocess.run([sys.executable, str(SCRIPT), "--results-dir", str(results)],
                                       capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("6 rows", completed.stdout)
            output = results / "comparison-summary.csv"
            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(6, len(rows))
            self.assertEqual([(str(cycle), mode) for cycle in (1, 2) for mode in MODES],
                             [(row["cycle"], row["mode"]) for row in rows])
            self.assertEqual("11", rows[0]["generatedTests"])
            self.assertEqual("55", rows[0]["evaluatedTests"])
            self.assertEqual("62.50", rows[0]["lineCoveragePct"])
            self.assertEqual("40.00", rows[0]["branchCoveragePct"])
            self.assertEqual("7", rows[1]["dynamoDbHeuristicsEvaluationCount"])
            self.assertEqual("3", rows[2]["dynamoDbInsertedRecords"])
            self.assertEqual("missing", rows[-1]["statistics_status"])
            self.assertEqual("", rows[-1]["generatedTests"])
            self.assertEqual("check coverage", rows[-1]["warnings"])
            self.assertIn("missing or empty statistics", completed.stderr)

    def test_empty_results_do_not_create_a_summary(self):
        """An empty FIFA tournament directory reports that no cycles were found."""
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary)
            completed = subprocess.run([sys.executable, str(SCRIPT), "--results-dir", str(results)],
                                       capture_output=True, text=True)
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("no cycle/mode runs found", completed.stderr)
            self.assertFalse((results / "comparison-summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
