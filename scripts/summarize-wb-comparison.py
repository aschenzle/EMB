#!/usr/bin/env python3
"""Summarize white-box comparison cycles into one CSV row per mode.

Example:
    python scripts/summarize-wb-comparison.py --results-dir results \
        --output results/comparison-summary.csv
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
CYCLE_NAME = re.compile(r"cycle-(\d+)$", re.IGNORECASE)
MODE_ORDER = {
    "no-ddb-heuristics": 0,
    "ddb-heuristics-no-insertions": 1,
    "ddb-heuristics-with-insertions": 2,
}
METRICS = (
    "generatedTests", "generatedTestTotalSize", "evaluatedTests", "evaluatedActions",
    "coveredTargets", "coveredLines", "numberOfLines", "lineCoveragePct",
    "coveredBranches", "numberOfBranches", "branchCoveragePct",
    "bootTimeCoveredTargets", "searchTimeCoveredTargets", "covered2xx",
    "distinct500Faults", "potentialFaults", "coverageFailures", "testTimeouts",
)
BASE_COLUMNS = ("app", "cycle", "mode", "seed", "evomaster_exit_code",
                "error", "warnings", "statistics_status", *METRICS)


def percentage(covered, total):
    """Return a percentage when both coverage counts are available."""
    try:
        denominator = int(total)
        return f"{100 * int(covered) / denominator:.2f}" if denominator else ""
    except (TypeError, ValueError):
        return ""


def read_statistics(path):
    """Read the last statistics row, since EvoMaster can append snapshots."""
    if not path.is_file():
        return {}, "missing"
    with path.open(newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        return {}, "empty"
    return rows[-1], "ok"


def read_metadata(path):
    """Read run metadata when present; older results may only have statistics."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def run_directories(results_dir):
    """Find mode folders in every application's numbered comparison cycles."""
    discovered = []
    for app_dir in results_dir.iterdir():
        if not app_dir.is_dir():
            continue
        for cycle_dir in app_dir.iterdir():
            match = CYCLE_NAME.fullmatch(cycle_dir.name)
            if not cycle_dir.is_dir() or match is None:
                continue
            for mode_dir in cycle_dir.iterdir():
                if mode_dir.is_dir() and any((mode_dir / name).is_file() for name in
                                             ("run.json", "statistics.csv", "controller.log", "evomaster.log")):
                    discovered.append((app_dir.name, int(match.group(1)), mode_dir))
    return sorted(discovered, key=lambda item: (item[0].lower(), item[1],
                                                MODE_ORDER.get(item[2].name, len(MODE_ORDER)),
                                                item[2].name.lower()))


def summarize(results_dir):
    """Build summary rows and collect every DynamoDB column in the raw statistics."""
    rows = []
    ddb_columns = set()
    for app, cycle, mode_dir in run_directories(results_dir):
        stats, status = read_statistics(mode_dir / "statistics.csv")
        metadata = read_metadata(mode_dir / "run.json")
        row = {"app": app, "cycle": cycle, "mode": mode_dir.name,
               "seed": metadata.get("seed", stats.get("seed", "")),
               "evomaster_exit_code": metadata.get("evomaster_exit_code", ""),
               "error": metadata.get("error") or "",
               "warnings": "; ".join(metadata.get("warnings") or []),
               "statistics_status": status}
        for metric in METRICS:
            row[metric] = stats.get(metric, "")
        row["lineCoveragePct"] = percentage(stats.get("coveredLines"), stats.get("numberOfLines"))
        row["branchCoveragePct"] = percentage(stats.get("coveredBranches"),
                                              stats.get("numberOfBranches"))
        for name, value in stats.items():
            if "dynamo" in name.lower() or "ddb" in name.lower():
                ddb_columns.add(name)
                row[name] = value
        rows.append(row)
    return rows, sorted(ddb_columns, key=str.lower)


def main():
    """Write a deterministic CSV summary without changing the raw run files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=PROJECT_DIR / "results",
                        help="root containing application/cycle/mode folders")
    parser.add_argument("--output", type=Path,
                        help="summary CSV path (default: <results-dir>/comparison-summary.csv)")
    args = parser.parse_args()
    results_dir = args.results_dir.resolve()
    if not results_dir.is_dir():
        parser.error(f"results directory does not exist: {results_dir}")
    rows, ddb_columns = summarize(results_dir)
    if not rows:
        parser.error(f"no cycle/mode runs found in {results_dir}")
    output = (args.output or results_dir / "comparison-summary.csv").resolve()
    if any(output == mode_dir / name for _, _, mode_dir in run_directories(results_dir)
           for name in ("run.json", "statistics.csv", "controller.log", "evomaster.log")):
        parser.error("output path points to a raw run file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=[*BASE_COLUMNS, *ddb_columns])
        writer.writeheader()
        writer.writerows(rows)
    missing = sum(row["statistics_status"] != "ok" for row in rows)
    print(f"Wrote {len(rows)} rows to {output}")
    if missing:
        print(f"Warning: {missing} run(s) have missing or empty statistics", file=sys.stderr)


if __name__ == "__main__":
    main()
