#!/usr/bin/env python3
"""Build selected white-box SUTs and their EvoMaster runners."""

import argparse
import csv
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
MANIFEST = SCRIPT_DIR / "dist-wb-apps.csv"
DIST = PROJECT_DIR / "dist"


@dataclass(frozen=True)
class App:
    """Describe the two projects and artifacts for one white-box app."""

    name: str
    jdk: str
    tool: str
    sut_module: str
    runner_module: str
    sut_jar_module: str

    def artifact_paths(self):
        """Return the SUT and runner JAR paths relative to the project root."""
        group = Path(f"jdk_{self.jdk}_{self.tool}")
        output_dir = Path("target") if self.tool == "maven" else Path("build/libs")
        sut_module = self.sut_jar_module or self.sut_module
        return (
            group / sut_module / output_dir / f"{self.name}-sut.jar",
            group / self.runner_module / output_dir / f"{self.name}-evomaster-runner.jar",
        )


def load_apps():
    """Load supported names and project locations from the manifest."""
    apps = {}
    with MANIFEST.open(newline="", encoding="utf-8") as manifest:
        reader = csv.DictReader(manifest)
        expected = {"name", "jdk", "tool", "sut_module", "runner_module", "sut_jar_module"}
        if set(reader.fieldnames or []) != expected:
            raise ValueError(f"Invalid columns in {MANIFEST}")
        for row in reader:
            app = App(**row)
            if app.name in apps:
                raise ValueError(f"Duplicate app in {MANIFEST}: {app.name}")
            if app.tool not in ("maven", "gradle"):
                raise ValueError(f"Invalid build tool for {app.name}: {app.tool}")
            group = PROJECT_DIR / f"jdk_{app.jdk}_{app.tool}"
            for module in (app.sut_module, app.runner_module, app.sut_jar_module):
                if module and not (group / module).is_dir():
                    raise ValueError(f"Missing module for {app.name}: {group / module}")
            if app.tool == "maven":
                for module in (app.sut_module, app.runner_module):
                    if not (group / module / "pom.xml").is_file():
                        raise ValueError(f"Missing pom.xml for {app.name}: {group / module}")
            apps[app.name] = app
    return apps


def build_command(group, apps):
    """Create one build command for the selected apps in a JDK/tool group."""
    jdk, tool = group
    modules = list(dict.fromkeys(
        module for app in apps for module in (app.sut_module, app.runner_module)
    ))
    if tool == "maven":
        return ["mvn", "-pl", ",".join(modules), "-am", "clean", "install", "-DskipTests"]
    wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
    tasks = [":" + module.replace("/", ":") + ":build" for module in modules]
    return [str(PROJECT_DIR / f"jdk_{jdk}_{tool}" / wrapper), *tasks, "-x", "test"]


def support_files():
    """Find the Java agent and JaCoCo JARs used by the full white-box build."""
    settings = SCRIPT_DIR / "build" / ".env"
    version = next(
        (line.split("=", 1)[1].strip() for line in settings.read_text(encoding="utf-8").splitlines()
         if line.startswith("EVOMASTER_VERSION=")),
        None,
    )
    if not version:
        raise ValueError(f"EVOMASTER_VERSION is missing from {settings}")
    agent = (Path.home() / ".m2" / "repository" / "org" / "evomaster"
             / "evomaster-client-java-instrumentation" / version
             / f"evomaster-client-java-instrumentation-{version}.jar")
    return (
        (agent, "evomaster-agent.jar"),
        (PROJECT_DIR / "jacoco" / "jacocoagent.jar", "jacocoagent.jar"),
        (PROJECT_DIR / "jacoco" / "jacococli.jar", "jacococli.jar"),
    )


def main():
    """Validate selection, build the chosen projects, and copy their JARs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="*", metavar="APP", help="app names from dist-wb-apps.csv")
    parser.add_argument("--list", action="store_true", help="list supported app names")
    parser.add_argument("--dry-run", action="store_true", help="show builds and artifacts without running them")
    args = parser.parse_args()

    try:
        available = load_apps()
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if args.list:
        print("\n".join(sorted(available)))
        return 0
    if not args.apps:
        parser.error("provide at least one app, or use --list")

    unknown = sorted({name for name in args.apps if name not in available})
    if unknown:
        parser.error(f"Unknown app(s): {', '.join(unknown)}. Use --list for valid names")

    selected = [available[name] for name in dict.fromkeys(args.apps)]
    groups = OrderedDict()
    for app in selected:
        groups.setdefault((app.jdk, app.tool), []).append(app)

    for group, group_apps in groups.items():
        command = build_command(group, group_apps)
        print(f"Build {', '.join(app.name for app in group_apps)}: {' '.join(command)}", flush=True)
        if args.dry_run:
            for app in group_apps:
                print("  Copy " + ", ".join(str(path) for path in app.artifact_paths()))

    if args.dry_run:
        return 0

    try:
        extra_files = support_files()
        for source, _ in extra_files:
            if not source.is_file():
                raise FileNotFoundError(f"Required file is missing: {source}")
        maven = shutil.which("mvn")
        if any(tool == "maven" for _, tool in groups) and not maven:
            raise FileNotFoundError("Maven is not on PATH")
        for jdk, tool in groups:
            java_home = os.environ.get(f"JAVA_HOME_{jdk}")
            if not java_home or not Path(java_home).is_dir():
                raise ValueError(f"JAVA_HOME_{jdk} must point to an installed JDK")
            if tool == "gradle" and not Path(build_command((jdk, tool), groups[(jdk, tool)])[0]).is_file():
                raise FileNotFoundError(f"Gradle wrapper is missing for JDK {jdk}")

        DIST.mkdir(exist_ok=True)
        for group, group_apps in groups.items():
            jdk, tool = group
            environment = os.environ.copy()
            environment["JAVA_HOME"] = os.environ[f"JAVA_HOME_{jdk}"]
            command = build_command(group, group_apps)
            if tool == "maven":
                command[0] = maven
            subprocess.run(command, cwd=PROJECT_DIR / f"jdk_{jdk}_{tool}", env=environment, check=True)
            for app in group_apps:
                for artifact in app.artifact_paths():
                    shutil.copy2(PROJECT_DIR / artifact, DIST)

        for source, name in extra_files:
            shutil.copy2(source, DIST / name)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
